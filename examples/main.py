import argparse
import yaml
import os
import torch

from pytorch_lightning import Trainer
from pytorch_lightning.loggers import WandbLogger, TensorBoardLogger

from examples.augmentations.supported import supported as supported_perturbations
from examples.audio_processing.spec_augment.supported import supported as supported_spec_perturbations
from examples.audio_processing.supported import supported as supported_preprocessors
from examples.losses.supported import supported as supported_losses
from examples.models.supported import supported_featurizers, supported_classifiers
from examples.callbacks.supported import supported_callbacks
from examples.exp_manager.exp_manager import exp_manager
from examples.exp_manager.logger import sr_logger

from examples.audio_processing.spec_augment.compose import Compose
from examples.algorithms.classification import SpeakerEmbeddingModel

from speech_shifts.datasets.mlsr_dataset import MLSRDataset
from speech_shifts.common.get_loaders import get_train_loader, get_eval_loader
from speech_shifts.common.audio.audio_augmentor import AudioAugmentor


def read_yaml(cfg_path):
    if cfg_path:
        with open(cfg_path) as f:
            cfg = yaml.load(f, Loader=yaml.FullLoader)
        return cfg

def build_spec_augmentor(spec_augmentations_config):
    perturbations = []
    if spec_augmentations_config:
        for aug_type, aug_config in spec_augmentations_config.items():
            if aug_type not in supported_spec_perturbations:
                sr_logger.info("\N{cross mark} Spectogram augmentation of type {} does not exist.".format(aug_type))
            else:
                if aug_config["do_augment"]:
                    aug_class = supported_spec_perturbations[aug_type]
                    aug_obj = aug_class(**aug_config["params"])
                    p = aug_config["p"]
                    perturbations.append((p, aug_obj))
                    sr_logger.info("\N{heavy check mark}  Spectogram augmentation of type {} is turned ON.".format(aug_type))
                else:
                    sr_logger.info("\N{cross mark} Spectogram augmentation of type {} is turned OFF.".format(aug_type))
    composed = Compose(perturbations)
    return composed
    
def build_augmentor(augmentations_config):
    perturbations = []
    if augmentations_config:
        for aug_type, aug_config in augmentations_config.items():
            if aug_type not in supported_perturbations:
                sr_logger.info(" \N{cross mark}Audio augmentation type of {} does not exist.".format(aug_type))
            else:
                if aug_config["do_augment"]:
                    aug_class = supported_perturbations[aug_type]
                    aug_obj = aug_class(**aug_config["params"])
                    p = aug_config["p"]
                    perturbations.append((p, aug_obj))
                    sr_logger.info("\N{heavy check mark}  Audio augmentation of type {} is turned ON.".format(aug_type))
                else:
                    sr_logger.info("\N{cross mark} Audio augmentation type of {} is turned OFF.".format(aug_type))
    augmentor = AudioAugmentor(perturbations=perturbations, mutex_perturbations=None)
    return augmentor

def build_preprocessor(preprocessor_config, eval=False):
    if preprocessor_config is None:
        raise ValueError("Preprocessor is missing.")
    else:
        spec_type = preprocessor_config["type"]
        spec_params = preprocessor_config["params"]
        spec_class = supported_preprocessors[spec_type]
        if eval:
            spec_params["dither"] = 0.0
        preprocessor = spec_class(**spec_params)
        sr_logger.info("\N{heavy check mark}  Preprocessor of type {} is CREATED.".format(type(preprocessor).__name__))
        return preprocessor

def build_loss(loss_config):
    if loss_config is None:
        raise ValueError("Loss is missing.")
    else:
        loss_type = loss_config["type"]
        loss_params = loss_config["params"]
        loss_class = supported_losses[loss_type]
        loss = loss_class(**loss_params)
        sr_logger.info("\N{heavy check mark}  Loss function of type {} is CREATED.".format(type(loss).__name__))
        return loss

def build_model(model_config, num_classess):
    if model_config is None:
        raise ValueError("Model is missing.")
    else:
        f_type = model_config["featurizer_type"]
        c_type = model_config.get("classifier_type", None)
        params = model_config["params"]
        cfg = read_yaml(params["cfg_path"])
        featurizer = supported_featurizers[f_type](**cfg["featurizer"])
        sr_logger.info("\N{heavy check mark}  Featurizer of type {} is CREATED.".format(type(featurizer).__name__))

        classifier = None
        if c_type is not None:
            classifier_params = cfg["classifier"]
            classifier_params.update(params)
            classifier_params["num_classes"] = num_classess
            classifier = supported_classifiers[c_type](**classifier_params)
            sr_logger.info("\N{heavy check mark}  Classifier (num_classes={}) of type {} is CREATED.".format(num_classess, type(classifier).__name__))
        return featurizer, classifier

def build_callbacks(callbacks_config, checkpoints_save_dir):
    callbacks = []
    if callbacks_config:
        for call_type, call_config in callbacks_config.items():
            if call_type not in supported_callbacks:
                sr_logger.info("Callback type {} does not exist.".format(call_type))
            else:
                if call_config["do_callback"]:
                    call_class = supported_callbacks[call_type]
                    if call_type == 'ModelCheckpoint':
                        call_obj = call_class(dirpath=checkpoints_save_dir, **call_config["params"])
                    else:
                        call_obj = call_class(**call_config["params"])
                    callbacks.append(call_obj)
                    sr_logger.info("\N{heavy check mark}  Callback of type {} is CREATED.".format(type(call_obj).__name__))
                else:
                    sr_logger.info("\N{cross mark} Callback type of {} is turned OFF.".format(call_type))

    return callbacks


def build_trainer(cfg, cfg_path):
    logger_path, checkpoints_path = exp_manager(cfg_path)
    callbacks = build_callbacks(read_yaml(cfg.get("callbacks", None)), checkpoints_path)
    #logger = WandbLogger(save_dir=logger_path, project="Speech-Shifts", name='', version='')
    logger = TensorBoardLogger(save_dir=logger_path, name='', version='')

    trainer = Trainer(**cfg["trainer"],
                        logger=logger,
                        callbacks=callbacks,
    )
    explicit_checkpoint_path = cfg["explicit_checkpoint_path"]
    if explicit_checkpoint_path and os.path.exists(explicit_checkpoint_path):
        sr_logger.info(f"Loading pytorch checkpoint from {explicit_checkpoint_path}")
        loaded_checkpoint = torch.load(explicit_checkpoint_path)
        trainer.fit_loop.global_step = loaded_checkpoint['global_step']
        trainer.fit_loop.current_epoch = loaded_checkpoint['epoch']
    
    sr_logger.info("\N{heavy check mark}  Logger of type {} is CREATED.".format(type(logger).__name__))
    sr_logger.info("\N{heavy check mark}  Trainer of type {} is CREATED.".format(type(trainer).__name__))
    return trainer

def build_dataloaders(data_config, augmentor):
    if data_config is None:
        raise ValueError("Data is missing.")
    
    dataset = MLSRDataset(data_config["root_dir"])
    loader_kwargs = {"type": "single_view"}
    val_dataset = dataset.get_subset("val", loader_kwargs=loader_kwargs, augmentor=None)
    id_val_dataset = dataset.get_subset("id_val", loader_kwargs=loader_kwargs, augmentor=None)
    test_dataset = dataset.get_subset("test", loader_kwargs=loader_kwargs, augmentor=None)

    params = data_config["params"]
    n_views = params["n_views"]
    if n_views > 1:
        loader_kwargs = {"type": "multi_view", "n_views": n_views}
    train_dataset = dataset.get_subset("train", loader_kwargs=loader_kwargs, augmentor=augmentor)

    val_dataloader = get_eval_loader("standard", val_dataset, batch_size=params["eval_bs"], num_workers=10)
    test_dataloader = get_eval_loader("standard", test_dataset, batch_size=params["eval_bs"])
    id_val_dataloader = get_eval_loader("standard", id_val_dataset, batch_size=params["eval_bs"], num_workers=10)
    train_dataloader = get_train_loader("standard", train_dataset, batch_size=params["train_bs"])

    sr_logger.info("\N{heavy check mark}  Train Dataset of type {} is CREATED.".format(type(train_dataset).__name__))
    sr_logger.info("\N{heavy check mark}  Val Dataset of type {} is CREATED.".format(type(val_dataset).__name__))
    sr_logger.info("\N{heavy check mark}  ID Val Dataset of type {} is CREATED.".format(type(id_val_dataset).__name__))

    return ((train_dataset, train_dataloader), 
            (val_dataset, val_dataloader),
            (id_val_dataset, id_val_dataloader))

def setup_env_vars():
    os.environ['ECCL_DEBUG'] = 'INFO'
    os.environ['NCCL_ASYNC_ERROR_HANDLING'] = '1'
    os.environ['NCCL_P2P_DISABLE'] = '1'

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Training speech_shifts models')
    parser.add_argument('--cfg', type=str, help='Config Path', required=False)
    args = parser.parse_args()

    cfg = read_yaml(args.cfg)
    setup_env_vars()

    augmentor = build_augmentor(read_yaml(cfg.get("audio_augmentations", None)))
    ((train_dataset, train_dataloader), 
     (val_dataset, val_dataloader),
     (id_val_dataset, id_val_dataloader)) = build_dataloaders(cfg.get("data", None), augmentor)
    
    num_classes = len(set(train_dataset.y_array.tolist()))
    featurizer, classifier = build_model(cfg.get("model", None), num_classes)

    spec_augmentor = build_spec_augmentor(read_yaml(cfg.get("spec_augmentations", None)))
    preprocessor = build_preprocessor(cfg.get("preprocessor", None))
    loss = build_loss(cfg.get("loss", None))
    trainer = build_trainer(cfg, args.cfg)


    model = SpeakerEmbeddingModel(
        preprocessor=preprocessor,
        train_spec_augmentor=spec_augmentor,
        featurizer=featurizer,
        classifier=classifier,
        loss=loss,
        optim_config=cfg["optimizer"]
    )
    model.setup_dataloaders(train_dataset, 
        train_dataloader, 
        val_dataset, 
        val_dataloader, 
        id_val_dataset, 
        id_val_dataloader
    )

    trainer.fit(model)


