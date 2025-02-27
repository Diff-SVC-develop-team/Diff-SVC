import logging
import os
import pathlib
import shutil
import sys
from datetime import datetime

import matplotlib

import utils
from utils.text_encoder import TokenTextEncoder

matplotlib.use('Agg')

import torch.utils.data
from torchmetrics import MeanMetric
import lightning.pytorch as pl
from lightning.pytorch.callbacks import LearningRateMonitor
from lightning.pytorch.loggers import TensorBoardLogger
from lightning.pytorch.utilities.rank_zero import rank_zero_debug, rank_zero_only

from basics.base_module import CategorizedModule
from utils.hparams import hparams
from utils.training_utils import (
    DsModelCheckpoint, DsTQDMProgressBar,
    DsBatchSampler, DsEvalBatchSampler,
    get_latest_checkpoint_path, get_strategy
)
from utils.phoneme_utils import locate_dictionary, build_phoneme_list

torch.multiprocessing.set_sharing_strategy(os.getenv('TORCH_SHARE_STRATEGY', 'file_system'))

log_format = '%(asctime)s %(message)s'
logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                    format=log_format, datefmt='%m/%d %I:%M:%S %p')


class BaseTask(pl.LightningModule):
    """
        Base class for training tasks.
        1. *load_ckpt*:
            load checkpoint;
        2. *training_step*:
            record and log the loss;
        3. *optimizer_step*:
            run backwards step;
        4. *start*:
            load training configs, backup code, log to tensorboard, start training;
        5. *configure_ddp* and *init_ddp_connection*:
            start parallel training.

        Subclasses should define:
        1. *build_model*, *build_optimizer*, *build_scheduler*:
            how to build the model, the optimizer and the training scheduler;
        2. *_training_step*:
            one training step of the model;
        3. *on_validation_end* and *_on_validation_end*:
            postprocess the validation output.
    """

    def __init__(self, *args, **kwargs):
        # dataset configs
        super().__init__(*args, **kwargs)
        self.loaded_optimizer_states_dict = {}
        self.example_input_array = None

        self.dataset_cls = None
        self.max_batch_frames = hparams['max_batch_frames']
        self.max_batch_size = hparams['max_batch_size']
        self.max_val_batch_frames = hparams['max_val_batch_frames']
        if self.max_val_batch_frames == -1:
            hparams['max_val_batch_frames'] = self.max_val_batch_frames = self.max_batch_frames
        self.max_val_batch_size = hparams['max_val_batch_size']
        if self.max_val_batch_size == -1:
            hparams['max_val_batch_size'] = self.max_val_batch_size = self.max_batch_size

        self.training_sampler = None
        self.model = None
        self.skip_immediate_validation = False
        self.skip_immediate_ckpt_save = False

        self.valid_metrics = {
            'total_loss': MeanMetric()
        }

    ###########
    # Training, validation and testing
    ###########
    def setup(self, stage):
        self.phone_encoder = self.build_phone_encoder()
        self.model = self.build_model()
        self.print_arch()
        self.build_losses()
        self.train_dataset = self.dataset_cls(hparams['train_set_name'])
        self.valid_dataset = self.dataset_cls(hparams['valid_set_name'])

    @staticmethod
    def build_phone_encoder():
        phone_list = build_phoneme_list()
        return TokenTextEncoder(vocab_list=phone_list)

    def build_model(self):
        raise NotImplementedError()

    @rank_zero_only
    def print_arch(self):
        utils.print_arch(self.model)

    def build_losses(self):
        raise NotImplementedError()

    def run_model(self, sample, infer=False):
        """
        steps:
            1. run the full model
            2. calculate losses if not infer
        """
        raise NotImplementedError()

    def on_train_epoch_start(self):
        if self.training_sampler is not None:
            self.training_sampler.set_epoch(self.current_epoch)

    def _training_step(self, sample):
        """
        :return: total loss: torch.Tensor, loss_log: dict, other_log: dict
        """
        losses = self.run_model(sample)
        total_loss = sum(losses.values())
        return total_loss, {**losses, 'batch_size': sample['size']}

    def training_step(self, sample, batch_idx, optimizer_idx=-1):
        total_loss, log_outputs = self._training_step(sample)

        # logs to progress bar
        self.log_dict(log_outputs, prog_bar=True, logger=False, on_step=True, on_epoch=False)
        self.log('lr', self.lr_schedulers().get_lr()[0], prog_bar=True, logger=False, on_step=True, on_epoch=False)
        # logs to tensorboard
        tb_log = {f'tr/{k}': v for k, v in log_outputs.items()}
        if self.global_step % self.trainer.log_every_n_steps == 0:
            self.logger.log_metrics(tb_log, step=self.global_step)

        return total_loss

    # def on_before_optimizer_step(self, *args, **kwargs):
    #     self.log_dict(grad_norm(self, norm_type=2))

    def _on_validation_start(self):
        pass

    def on_validation_start(self):
        self._on_validation_start()
        for metric in self.valid_metrics.values():
            metric.to(self.device)
            metric.reset()

    def _validation_step(self, sample, batch_idx):
        """

        :param sample:
        :param batch_idx:
        :return: loss_log: dict, weight: int
        """
        raise NotImplementedError()

    def validation_step(self, sample, batch_idx):
        """

        :param sample:
        :param batch_idx:
        """
        if self.skip_immediate_validation:
            rank_zero_debug(f"Skip validation {batch_idx}")
            return {}
        with torch.autocast(self.device.type, enabled=False):
            outputs, weight = self._validation_step(sample, batch_idx)
        for k, v in outputs.items():
            if isinstance(self.valid_metrics[k], MeanMetric):
                self.valid_metrics[k].update(v, weight=weight)
        return outputs

    def on_validation_epoch_end(self):
        if self.skip_immediate_validation:
            self.skip_immediate_validation = False
            self.skip_immediate_ckpt_save = True
            return
        metric_vals = {k: v.compute() for k, v in self.valid_metrics.items()}
        self.log('val_loss', metric_vals['total_loss'], on_epoch=True, prog_bar=True, logger=False)
        self.logger.log_metrics({f'val/{k}': v for k, v in metric_vals.items()}, step=self.global_step)
        for metric in self.valid_metrics.values():
            metric.reset()

    # noinspection PyMethodMayBeStatic
    def build_scheduler(self, optimizer):
        # return WarmupCosineSchedule(optimizer,
        #                             warmup_steps=hparams['warmup_updates'],
        #                             t_total=hparams['max_updates'],
        #                             eta_min=0)
        return torch.optim.lr_scheduler.StepLR(
            optimizer, step_size=hparams['lr_decay_steps'], gamma=hparams['lr_decay_gamma']
        )

    # noinspection PyMethodMayBeStatic
    def build_optimizer(self, model):
        optimizer = torch.optim.AdamW(
            filter(lambda p: p.requires_grad, model.parameters()),
            lr=hparams['lr'],
            betas=(hparams['optimizer_adam_beta1'], hparams['optimizer_adam_beta2']),
            weight_decay=hparams['weight_decay'])
        return optimizer

    def configure_optimizers(self):
        optm = self.build_optimizer(self.model)
        scheduler = self.build_scheduler(optm)
        if scheduler is None:
            return optm
        return {
            "optimizer": optm,
            "lr_scheduler": {
                "scheduler": scheduler,
                "interval": "step",
                "frequency": 1
            }
        }

    def train_dataloader(self):
        self.training_sampler = DsBatchSampler(
            self.train_dataset,
            max_batch_frames=self.max_batch_frames,
            max_batch_size=self.max_batch_size,
            num_replicas=(self.trainer.distributed_sampler_kwargs or {}).get('num_replicas', 1),
            rank=(self.trainer.distributed_sampler_kwargs or {}).get('rank', 0),
            sort_by_similar_size=hparams['sort_by_len'],
            required_batch_count_multiple=hparams['accumulate_grad_batches'],
            shuffle_sample=True,
            shuffle_batch=False,
            seed=hparams['seed']
        )
        return torch.utils.data.DataLoader(self.train_dataset,
                                           collate_fn=self.train_dataset.collater,
                                           batch_sampler=self.training_sampler,
                                           num_workers=hparams['ds_workers'],
                                           prefetch_factor=hparams['dataloader_prefetch_factor'],
                                           pin_memory=True,
                                           persistent_workers=True)

    def val_dataloader(self):
        sampler = DsEvalBatchSampler(
            self.valid_dataset,
            max_batch_frames=self.max_val_batch_frames,
            max_batch_size=self.max_val_batch_size,
            rank=(self.trainer.distributed_sampler_kwargs or {}).get('rank', 0),
            batch_by_size=False
        )
        return torch.utils.data.DataLoader(self.valid_dataset,
                                           collate_fn=self.valid_dataset.collater,
                                           batch_sampler=sampler,
                                           num_workers=hparams['ds_workers'],
                                           prefetch_factor=hparams['dataloader_prefetch_factor'],
                                           shuffle=False)

    def test_dataloader(self):
        return self.val_dataloader()

    def on_test_start(self):
        self.on_validation_start()

    def test_step(self, sample, batch_idx):
        return self.validation_step(sample, batch_idx)

    def on_test_end(self):
        return self.on_validation_end()

    ###########
    # Running configuration
    ###########

    @classmethod
    def start(cls):
        pl.seed_everything(hparams['seed'], workers=True)
        task = cls()
        work_dir = pathlib.Path(hparams['work_dir'])
        trainer = pl.Trainer(
            accelerator=hparams['pl_trainer_accelerator'],
            devices=hparams['pl_trainer_devices'],
            num_nodes=hparams['pl_trainer_num_nodes'],
            strategy=get_strategy(
                accelerator=hparams['pl_trainer_accelerator'],
                devices=hparams['pl_trainer_devices'],
                num_nodes=hparams['pl_trainer_num_nodes'],
                strategy=hparams['pl_trainer_strategy'],
                backend=hparams['ddp_backend']
            ),
            precision=hparams['pl_trainer_precision'],
            callbacks=[
                DsModelCheckpoint(
                    dirpath=work_dir,
                    filename='model_ckpt_steps_{step}',
                    auto_insert_metric_name=False,
                    monitor='step',
                    mode='max',
                    save_last=False,
                    # every_n_train_steps=hparams['val_check_interval'],
                    save_top_k=hparams['num_ckpt_keep'],
                    permanent_ckpt_start=hparams['permanent_ckpt_start'],
                    permanent_ckpt_interval=hparams['permanent_ckpt_interval'],
                    verbose=True
                ),
                LearningRateMonitor(logging_interval='step'),
                DsTQDMProgressBar(),
            ],
            logger=TensorBoardLogger(
                save_dir=str(work_dir),
                name='lightning_logs',
                version='lastest'
            ),
            gradient_clip_val=hparams['clip_grad_norm'],
            val_check_interval=hparams['val_check_interval'] * hparams['accumulate_grad_batches'],
            # so this is global_steps
            check_val_every_n_epoch=None,
            log_every_n_steps=hparams['log_interval'],
            max_steps=hparams['max_updates'],
            use_distributed_sampler=False,
            num_sanity_val_steps=hparams['num_sanity_val_steps'] if not hparams['validate'] else 10000,
            accumulate_grad_batches=hparams['accumulate_grad_batches']
        )
        if not hparams['infer']:  # train
            @rank_zero_only
            def train_payload_copy():
                # copy_code = input(f'{hparams["save_codes"]} code backup? y/n: ') == 'y'
                copy_code = True  # backup code every time
                if copy_code:
                    code_dir = work_dir / 'codes' / datetime.now().strftime('%Y%m%d%H%M%S')
                    code_dir.mkdir(exist_ok=True, parents=True)
                    for c in hparams['save_codes']:
                        shutil.copytree(c, code_dir, dirs_exist_ok=True)
                    print(f'| Copied codes to {code_dir}.')
                # Copy spk_map.json and dictionary.txt to work dir
                binary_dir = pathlib.Path(hparams['binary_data_dir'])
                spk_map = work_dir / 'spk_map.json'
                spk_map_src = binary_dir / 'spk_map.json'
                if not spk_map.exists() and spk_map_src.exists():
                    shutil.copy(spk_map_src, spk_map)
                    print(f'| Copied spk map to {spk_map}.')
                dictionary = work_dir / 'dictionary.txt'
                dict_src = binary_dir / 'dictionary.txt'
                if not dictionary.exists():
                    if dict_src.exists():
                        shutil.copy(dict_src, dictionary)
                    else:
                        shutil.copy(locate_dictionary(), dictionary)
                    print(f'| Copied dictionary to {dictionary}.')

            train_payload_copy()
            trainer.fit(task, ckpt_path=get_latest_checkpoint_path(work_dir))
        else:
            trainer.test(task)

    def on_save_checkpoint(self, checkpoint):
        if isinstance(self.model, CategorizedModule):
            checkpoint['category'] = self.model.category
        checkpoint['trainer_stage'] = self.trainer.state.stage.value

    def on_load_checkpoint(self, checkpoint):
        from lightning.pytorch.trainer.states import RunningStage
        if checkpoint.get('trainer_stage', '') == RunningStage.VALIDATING.value:
            self.skip_immediate_validation = True
