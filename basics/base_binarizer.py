import json
import logging
import os
import pathlib
import random
import shutil
from copy import deepcopy

import numpy as np
import torch
from tqdm import tqdm

from utils.hparams import hparams
from utils.indexed_datasets import IndexedDatasetBuilder
from utils.multiprocess_utils import chunked_multiprocess_run
from utils.phoneme_utils import build_phoneme_list, locate_dictionary
from utils.text_encoder import TokenTextEncoder


class BinarizationError(Exception):
    pass


class BaseBinarizer:
    """
        Base class for data processing.
        1. *process* and *process_data_split*:
            process entire data, generate the train-test split (support parallel processing);
        2. *process_item*:
            process singe piece of data;
        3. *get_pitch*:
            infer the pitch using some algorithm;
        4. *get_align*:
            get the alignment using 'mel2ph' format (see https://arxiv.org/abs/1905.09263).
        5. phoneme encoder, voice encoder, etc.

        Subclasses should define:
        1. *load_metadata*:
            how to read multiple datasets from files;
        2. *train_item_names*, *valid_item_names*, *test_item_names*:
            how to split the dataset;
        3. load_ph_set:
            the phoneme set.
    """

    def __init__(self, data_dir=None, data_attrs=None):
        if data_dir is None:
            data_dir = hparams['raw_data_dir']
        if not isinstance(data_dir, list):
            data_dir = [data_dir]

        speakers = hparams['speakers']
        assert isinstance(speakers, list), 'Speakers must be a list'
        assert len(speakers) == len(set(speakers)), 'Speakers cannot contain duplicate names'

        self.raw_data_dirs = [pathlib.Path(d) for d in data_dir]
        self.binary_data_dir = pathlib.Path(hparams['binary_data_dir'])
        self.data_attrs = [] if data_attrs is None else data_attrs

        if hparams['use_spk_id']:
            assert len(speakers) == len(self.raw_data_dirs), \
                'Number of raw data dirs must equal number of speaker names!'

        self.binarization_args = hparams['binarization_args']
        self.augmentation_args = hparams.get('augmentation_args', {})
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

        self.spk_map = None
        self.items = {}
        self.phone_encoder = TokenTextEncoder(vocab_list=build_phoneme_list())
        self.timestep = hparams['hop_size'] / hparams['audio_sample_rate']

        # load each dataset
        for ds_id, data_dir in enumerate(self.raw_data_dirs):
            self.load_meta_data(pathlib.Path(data_dir), ds_id)
        self.item_names = sorted(list(self.items.keys()))
        self._train_item_names, self._valid_item_names = self.split_train_valid_set()

        if self.binarization_args['shuffle']:
            random.seed(hparams['seed'])
            random.shuffle(self.item_names)

    def load_meta_data(self, raw_data_dir: pathlib.Path, ds_id):
        raise NotImplementedError()

    def split_train_valid_set(self):
        """
        Split the dataset into training set and validation set.
        :return: train_item_names, valid_item_names
        """
        item_names = set(deepcopy(self.item_names))
        prefixes = set([str(pr) for pr in hparams['test_prefixes']])
        valid_item_names = set()
        # Add prefixes that specified speaker index and matches exactly item name to test set
        for prefix in deepcopy(prefixes):
            if prefix in item_names:
                valid_item_names.add(prefix)
                prefixes.remove(prefix)
        # Add prefixes that exactly matches item name without speaker id to test set
        for prefix in deepcopy(prefixes):
            for name in item_names:
                if name.split(':')[-1] == prefix:
                    valid_item_names.add(name)
                    prefixes.remove(prefix)
        # Add names with one of the remaining prefixes to test set
        for prefix in deepcopy(prefixes):
            for name in item_names:
                if name.startswith(prefix):
                    valid_item_names.add(name)
                    prefixes.remove(prefix)
        for prefix in prefixes:
            for name in item_names:
                if name.split(':')[-1].startswith(prefix):
                    valid_item_names.add(name)
        valid_item_names = sorted(list(valid_item_names))
        train_item_names = [x for x in item_names if x not in set(valid_item_names)]
        logging.info("train {}".format(len(train_item_names)))
        logging.info("test {}".format(len(valid_item_names)))
        return train_item_names, valid_item_names

    @property
    def train_item_names(self):
        return self._train_item_names

    @property
    def valid_item_names(self):
        return self._valid_item_names

    def build_spk_map(self):
        spk_map = {x: i for i, x in enumerate(hparams['speakers'])}
        assert len(spk_map) <= hparams['num_spk'], 'Actual number of speakers should be smaller than num_spk!'
        self.spk_map = spk_map

    def meta_data_iterator(self, prefix):
        if prefix == 'train':
            item_names = self.train_item_names
        else:
            item_names = self.valid_item_names
        for item_name in item_names:
            meta_data = self.items[item_name]
            yield item_name, meta_data

    def process(self):
        os.makedirs(hparams['binary_data_dir'], exist_ok=True)

        # Copy spk_map and dictionary to binary data dir
        self.build_spk_map()
        print("| spk_map: ", self.spk_map)
        spk_map_fn = f"{hparams['binary_data_dir']}/spk_map.json"
        json.dump(self.spk_map, open(spk_map_fn, 'w', encoding='utf-8'))
        shutil.copy(locate_dictionary(), self.binary_data_dir / 'dictionary.txt')
        self.check_coverage()

        # Process train set and valid set
        self.process_dataset('valid')
        self.process_dataset(
            'train',
            num_workers=int(self.binarization_args['num_workers']),
            apply_augmentation=len(self.augmentation_args) > 0
        )

    def check_coverage(self):
        raise NotImplementedError()

    def process_dataset(self, prefix, num_workers=0, apply_augmentation=False):
        args = []
        builder = IndexedDatasetBuilder(self.binary_data_dir, prefix=prefix, allowed_attr=self.data_attrs)
        lengths = []
        total_sec = 0
        total_raw_sec = 0

        for item_name, meta_data in self.meta_data_iterator(prefix):
            args.append([item_name, meta_data, self.binarization_args])

        aug_map = self.arrange_data_augmentation(self.meta_data_iterator(prefix)) if apply_augmentation else {}

        def postprocess(_item):
            nonlocal total_sec, total_raw_sec
            if _item is None:
                return
            builder.add_item(_item)
            lengths.append(_item['length'])
            total_sec += _item['seconds']
            total_raw_sec += _item['seconds']

            for task in aug_map.get(_item['name'], []):
                aug_item = task['func'](_item, **task['kwargs'])
                builder.add_item(aug_item)
                lengths.append(aug_item['length'])
                total_sec += aug_item['seconds']

        if num_workers > 0:
            # code for parallel processing
            for item in tqdm(
                    chunked_multiprocess_run(self.process_item, args, num_workers=num_workers),
                    total=len(list(self.meta_data_iterator(prefix)))
            ):
                postprocess(item)
        else:
            # code for single cpu processing
            for a in tqdm(args):
                item = self.process_item(*a)
                postprocess(item)

        builder.finalize()
        with open(self.binary_data_dir / f'{prefix}.lengths', 'wb') as f:
            # noinspection PyTypeChecker
            np.save(f, lengths)

        if apply_augmentation:
            print(f'| {prefix} total duration (before augmentation): {total_raw_sec:.2f}s')
            print(
                f'| {prefix} total duration (after augmentation): {total_sec:.2f}s ({total_sec / total_raw_sec:.2f}x)')
        else:
            print(f'| {prefix} total duration: {total_raw_sec:.2f}s')

    def arrange_data_augmentation(self, prefix):
        """
        Code for all types of data augmentation should be added here.
        """
        raise NotImplementedError()

    def process_item(self, item_name, meta_data, binarization_args):
        raise NotImplementedError()
