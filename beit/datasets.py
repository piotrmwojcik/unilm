# --------------------------------------------------------
# BEIT: BERT Pre-Training of Image Transformers (https://arxiv.org/abs/2106.08254)
# Github source: https://github.com/microsoft/unilm/tree/master/beit
# Copyright (c) 2021 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# By Hangbo Bao
# Based on timm, DINO and DeiT code bases
# https://github.com/rwightman/pytorch-image-models/tree/master/timm
# https://github.com/facebookresearch/deit/
# https://github.com/facebookresearch/dino
# --------------------------------------------------------'
import os
import torch
import numpy as np
import random

from torchvision import datasets, transforms

from timm.data.constants import \
    IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD, IMAGENET_INCEPTION_MEAN, IMAGENET_INCEPTION_STD

from beit.dataset_folder_with_segmentation import SegmentedImageFolder, pil_pkl_loader_classes, pil_pkl_loader
from transforms import RandomResizedCropAndInterpolationWithTwoPic, RandomHorizontalFlip
import torchvision.transforms.functional as F
from timm.data import create_transform

from dall_e.utils import map_pixels
from masking_generator import MaskingGenerator
from dataset_folder import ImageFolder


class DataAugmentationForBEiT(object):
    def __init__(self, args):
        imagenet_default_mean_and_std = args.imagenet_default_mean_and_std
        mean = IMAGENET_INCEPTION_MEAN if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_MEAN
        std = IMAGENET_INCEPTION_STD if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_STD

        self.patch_size = args.patch_size
        self.num_boxes = args.num_boxes
        self.instance_size = args.instance_size

        self.common_transform = transforms.Compose([
            transforms.ColorJitter(0.4, 0.4, 0.4),
        ])

        self.crop_and_resize = RandomResizedCropAndInterpolationWithTwoPic(
            size=args.input_size, second_size=args.second_input_size,
            interpolation=args.train_interpolation, second_interpolation=args.second_interpolation,
        )

        self.random_hflip = RandomHorizontalFlip(p=0.5)

        self.normalize = transforms.Normalize(
            mean=torch.tensor(mean),
            std=torch.tensor(std))

        self.patch_transform = transforms.Compose([
            transforms.ToTensor(),
            self.normalize
        ])

        if args.discrete_vae_type == "dall-e":
            self.visual_token_transform = transforms.Compose([
                transforms.ToTensor(),
                map_pixels,
            ])
        elif args.discrete_vae_type == "customized":
            self.visual_token_transform = transforms.Compose([
                transforms.ToTensor(),
                self.normalize
            ])
        else:
            raise NotImplementedError()

        self.masked_position_generator = MaskingGenerator(
            args.window_size, num_masking_patches=args.num_mask_patches,
            max_num_patches=args.max_mask_patches_per_block,
            min_num_patches=args.min_mask_patches_per_block,
        )

    @staticmethod
    def _unzip(zip_list):
        return [l[0] for l in zip_list]

    @staticmethod
    def _get_indices(boolean_list, value):
        idx = list(filter(lambda x: x[1] == value, enumerate(boolean_list)))
        return DataAugmentationForBEiT._unzip(idx)

    def _masks_for_masked_boxes(self, boxes, mask, patch_size):
        if boxes.numel() == 0:
            return None
        bmask = []
        for i in range(boxes.shape[0]):
            if boxes[i, 0] == -1:
                bmask.append(torch.tensor(False).unsqueeze(0))
                continue
            scaled_box = boxes[i] // patch_size[0]
            crop = mask[scaled_box[1]:scaled_box[3] + 1,
                        scaled_box[0]:scaled_box[2] + 1]
            bmask.append(torch.any(torch.tensor(crop, dtype=torch.bool)).unsqueeze(0))
        return torch.cat(bmask, dim=0)

    def _attention_mask(self, boxes, boxes_mask, num_boxes):
        fake_box = torch.tensor([-1, -1, -1, -1])
        if boxes_mask is None:
            return fake_box.expand(num_boxes, -1), torch.tensor([False] * num_boxes)

        masked_boxes = DataAugmentationForBEiT._get_indices(boxes_mask, True)
        unmasked_boxes = DataAugmentationForBEiT._get_indices(boxes_mask, False)

        boxes_available = boxes.shape[0]
        if boxes_available == num_boxes:
            return boxes, torch.tensor([True] * num_boxes)
        if boxes_available < num_boxes:
            padding_length = num_boxes - boxes_available
            fake_box = fake_box.expand(padding_length, -1)
            attention_mask = [True] * boxes_available + [False] * padding_length
            return torch.cat([boxes, fake_box]), torch.tensor(attention_mask)
        if boxes_available > num_boxes >= len(masked_boxes):
            remaining_unmasked = num_boxes - len(masked_boxes)
            idx = random.sample(unmasked_boxes, remaining_unmasked)
            return torch.cat([boxes[masked_boxes], boxes[idx]]), torch.tensor([True] * num_boxes)
        if len(masked_boxes) == num_boxes:
            return boxes[masked_boxes], torch.tensor([True] * num_boxes)
        if num_boxes < len(masked_boxes):
            idx = random.sample(masked_boxes, num_boxes)
            return boxes[idx], torch.tensor([True] * num_boxes)

    def _crops(self, boxes, img, size):
        crops = []
        image = transforms.ToTensor()(img)
        for i in range(boxes.shape[0]):
            if boxes[i, 1] == -1:
                crop = torch.rand(3, size[0], size[1])
            else:
                crop = image[:, int(boxes[i, 1]): int(boxes[i, 3]), int(boxes[i, 0]): int(boxes[i, 2])]
                crop = F.resize(crop, size=size)
                crop = map_pixels(crop)
            crops.append(crop.unsqueeze(dim=0).float())
        return torch.cat(crops, dim=0)

    def __call__(self, image, boxes=None):
        for_patches = self.common_transform(image)
        for_patches = self.random_hflip(for_patches, boxes=boxes)
        if isinstance(for_patches, tuple):
            for_patches, boxes = for_patches
        for_patches, for_visual_tokens = self.crop_and_resize(img=for_patches, boxes=boxes)
        mask = self.masked_position_generator()
        if isinstance(for_patches, tuple):
            for_patches, boxes = for_patches
            boxes_mask = self._masks_for_masked_boxes(boxes, mask, self.patch_size)
            boxes, attention_mask = self._attention_mask(boxes, boxes_mask, self.num_boxes)
            crops = self._crops(boxes, for_patches, (self.instance_size, self.instance_size))
            if boxes_mask is None:
                boxes_mask = torch.zeros(self.num_boxes).bool()
            else:
                boxes_mask = self._masks_for_masked_boxes(boxes, mask, self.patch_size)
            return \
                self.patch_transform(for_patches), boxes, self.visual_token_transform(for_visual_tokens), \
                crops, mask, attention_mask, boxes_mask
        else:
            return \
                self.patch_transform(for_patches), self.visual_token_transform(for_visual_tokens), \
                mask

    def __repr__(self):
        repr = "(DataAugmentationForBEiT,\n"
        repr += "  common_transform = %s,\n" % str(self.common_transform)
        repr += "  patch_transform = %s,\n" % str(self.patch_transform)
        repr += "  visual_tokens_transform = %s,\n" % str(self.visual_token_transform)
        repr += "  Masked position generator = %s,\n" % str(self.masked_position_generator)
        repr += ")"
        return repr


def merge_classes(classes):
    classes = classes.type(torch.int64) - 1

    # epithelial class
    classes[classes == 3] = 2

    # spindle-shaped
    classes[classes == 4] = 3
    classes[classes == 5] = 3
    classes[classes == 6] = 3

    return classes


class DataAugmentationForBEITDatasetEval(object):
    def __init__(self, args):
        self.is_conesp = 'CoNSeP' in args.data_path

        imagenet_default_mean_and_std = args.imagenet_default_mean_and_std
        mean = IMAGENET_INCEPTION_MEAN if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_MEAN
        std = IMAGENET_INCEPTION_STD if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_STD

        self.patch_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=torch.tensor(mean), std=torch.tensor(std))])

    def __call__(self, image, boxes=None):
        assert(boxes is None or isinstance(boxes, tuple))
        boxes, classes = boxes
        if self.is_conesp:
            classes = merge_classes(classes)
        return [self.patch_transform(image), transforms.ToTensor()(image), (boxes, classes)]


class DataAugmentationForBEITDatasetFinetune(object):

    def __init__(self, args, finetune=False):
        self.num_boxes = args.num_boxes
        self.is_conesp = 'CoNSeP' in args.data_path
        self.finetune = finetune
        self.random_hflip = RandomHorizontalFlip(p=0.5)
        self.common_transform = transforms.Compose([
            transforms.ColorJitter(0.4, 0.4, 0.4),
        ])

        self.crop_and_resize = RandomResizedCropAndInterpolationWithTwoPic(
            size=args.input_size, second_size=None,
            interpolation='bicubic', second_interpolation='lanczos',
        )

        imagenet_default_mean_and_std = args.imagenet_default_mean_and_std
        mean = IMAGENET_INCEPTION_MEAN if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_MEAN
        std = IMAGENET_INCEPTION_STD if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_STD

        self.patch_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(
                mean=torch.tensor(mean),
                std=torch.tensor(std))])

        if not self.finetune:
            self.masked_position_generator = MaskingGenerator(
                args.window_size, num_masking_patches=args.num_mask_patches,
                max_num_patches=args.max_mask_patches_per_block,
                min_num_patches=args.min_mask_patches_per_block,
            )

    def __call__(self, image, boxes=None):
        assert(boxes is None or isinstance(boxes, tuple))

        if self.finetune:
            fake_box = torch.tensor([-1, -1, -1, -1])
            fake_class = torch.tensor([-1])

            if boxes is None:
                return [self.patch_transform(image),
                        transforms.ToTensor()(image),
                        torch.tensor([True] * self.num_boxes),
                        (fake_box.expand(self.num_boxes, -1), fake_class.expand(self.num_boxes, -1))]
            else:
                boxes, classes = boxes
                if self.is_conesp:
                    classes = merge_classes(classes)

                image, boxes = self.random_hflip(image, boxes)
                image, boxes, classes = self.crop_and_resize(image, boxes, classes)
                image = self.common_transform(image)

                boxes_available = boxes.shape[0]

                if boxes.shape[0] <= self.num_boxes:
                    padding_length = self.num_boxes - boxes_available
                    fake_box = fake_box.expand(padding_length, -1)
                    fake_class = fake_class.expand(padding_length)
                    return [self.patch_transform(image),
                            transforms.ToTensor()(image),
                            torch.tensor([True] * boxes_available + [False] * padding_length),
                            (torch.cat([boxes, fake_box]), torch.cat([classes, fake_class]))]
                else:
                    idx = random.sample(range(boxes_available), self.num_boxes)
                    return [self.patch_transform(image),
                            transforms.ToTensor()(image),
                            torch.tensor([True] * self.num_boxes),
                            (boxes[idx], classes[idx])]
        else:
            return [self.patch_transform(image),
                    transforms.ToTensor()(image),
                    self.masked_position_generator(),
                    boxes]


def build_beit_pretraining_dataset(args):
    transform = DataAugmentationForBEiT(args)
    print("Data Aug = %s" % str(transform))
    if args.data_set == 'CIFAR':
        return datasets.CIFAR100(root=args.data_path, train=True, transform=transform, download=True)
    else:
        return ImageFolder(args.data_path, transform=transform)


def build_instaformer_pretraining_dataset(args):
    transform = DataAugmentationForBEiT(args)
    print("Data Aug = %s" % str(transform))
    return SegmentedImageFolder(root=args.data_path, loader=pil_pkl_loader,
                                transform=transform)


def build_instaformer_eval_dataset(args, data_root=None):
    transform = DataAugmentationForBEITDatasetEval(args)
    print("Data Aug = %s" % str(transform))
    if data_root is None:
        data_root = args.data_path
    return SegmentedImageFolder(root=data_root, loader=pil_pkl_loader_classes,
                                transform=transform)


def build_instaformer_dataset(args, finetune=False, data_root=None):
    transform = DataAugmentationForBEITDatasetFinetune(args, finetune=finetune)
    if data_root is None:
        data_root = args.data_path
    return SegmentedImageFolder(root=data_root, loader=pil_pkl_loader_classes,
                                transform=transform)


def build_dataset(is_train, args):
    transform = build_transform(is_train, args)

    print("Transform = ")
    if isinstance(transform, tuple):
        for trans in transform:
            print(" - - - - - - - - - - ")
            for t in trans.transforms:
                print(t)
    else:
        for t in transform.transforms:
            print(t)
    print("---------------------------")

    if args.data_set == 'CIFAR':
        dataset = datasets.CIFAR100(root=args.data_path, train=True, transform=transform, download=False)
        nb_classes = 100
    elif args.data_set == 'IMNET':
        root = os.path.join(args.data_path, 'train' if is_train else 'val')
        dataset = datasets.ImageFolder(root, transform=transform)
        nb_classes = 1000
    elif args.data_set == "image_folder":
        root = args.data_path if is_train else args.eval_data_path
        dataset = ImageFolder(root, transform=transform)
        nb_classes = args.nb_classes
        assert len(dataset.class_to_idx) == nb_classes
    else:
        raise NotImplementedError()
    assert nb_classes == args.nb_classes
    print("Number of the class = %d" % args.nb_classes)

    return dataset, nb_classes


def build_transform(is_train, args):
    resize_im = args.input_size > 32
    imagenet_default_mean_and_std = args.imagenet_default_mean_and_std
    mean = IMAGENET_INCEPTION_MEAN if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_MEAN
    std = IMAGENET_INCEPTION_STD if not imagenet_default_mean_and_std else IMAGENET_DEFAULT_STD

    if is_train:
        # this should always dispatch to transforms_imagenet_train
        transform = create_transform(
            input_size=args.input_size,
            is_training=True,
            color_jitter=args.color_jitter,
            auto_augment=args.aa,
            interpolation=args.train_interpolation,
            re_prob=args.reprob,
            re_mode=args.remode,
            re_count=args.recount,
            mean=mean,
            std=std,
        )
        if not resize_im:
            # replace RandomResizedCropAndInterpolation with
            # RandomCrop
            transform.transforms[0] = transforms.RandomCrop(
                args.input_size, padding=4)
        return transform

    t = []
    if resize_im:
        if args.crop_pct is None:
            if args.input_size < 384:
                args.crop_pct = 224 / 256
            else:
                args.crop_pct = 1.0
        size = int(args.input_size / args.crop_pct)
        t.append(
            transforms.Resize(size, interpolation=3),  # to maintain same ratio w.r.t. 224 images
        )
        t.append(transforms.CenterCrop(args.input_size))

    t.append(transforms.ToTensor())
    t.append(transforms.Normalize(mean, std))
    return transforms.Compose(t)
