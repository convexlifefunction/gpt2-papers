import torch
import json
import logging
import math
import os
import random
import dataclasses
from dataclasses import dataclass, field
from typing import Optional
import collections
from dict_to_obj import DictToObj
import warnings

warnings.filterwarnings("ignore")
from timeit import default_timer as timer
import wandb

wandb.login()


from transformers import (
    CONFIG_MAPPING,
    MODEL_WITH_LM_HEAD_MAPPING,
    AutoConfig,
    AutoModelWithLMHead,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    LineByLineTextDataset,
    PreTrainedTokenizer,
    TextDataset,
    Trainer,
    TrainingArguments,
    set_seed,
)

sep = "<|sep|>"


def get_dataset(args, tokenizer, evaluate=False):
    file_path = args.eval_data_file if evaluate else args.train_data_file
    if args.line_by_line:
        return LineByLineTextDataset(
            tokenizer=tokenizer, file_path=file_path, block_size=args.block_size
        )
    else:
        return TextDataset(
            tokenizer=tokenizer, file_path=file_path, block_size=args.block_size
        )


def main():
    # Logging
    logger = logging.getLogger(__name__)
    # Model classes
    MODEL_CONFIG_CLASSES = list(MODEL_WITH_LM_HEAD_MAPPING.keys())
    MODEL_TYPES = tuple(conf.model_type for conf in MODEL_CONFIG_CLASSES)
    # Model arguments
    # Model arguments
    model_args = collections.defaultdict(
        config_name=None,
        model_name_or_path="gpt2",
        model_type="gpt2",
        tokenizer_name=None,
        cache_dir=None,
    )
    # Data arguments
    data_args = collections.defaultdict(
        train_data_file="/data/train.txt",
        eval_data_file="/data/eval.txt",
        line_by_line=False,
        mlm=False,
        mlm_probability=0.15,
        block_size=512,
        overwrite_cache=False,
    )
    # Training arguments
    training_args = TrainingArguments(
        output_dir="/gpt2",
        overwrite_output_dir=True,
        do_train=True,
        do_eval=True,
        do_predict=False,
        evaluate_during_training=True,
        per_gpu_train_batch_size=1,
        per_gpu_eval_batch_size=1,
        gradient_accumulation_steps=1,
        learning_rate=5e-5,
        weight_decay=0.0,
        adam_epsilon=1e-08,
        max_grad_norm=1.0,
        num_train_epochs=5.0,
        max_steps=-1,
        warmup_steps=0,
        logging_dir=None,
        logging_first_step=False,
        logging_steps=10000,
        save_steps=10000,
        save_total_limit=10,
        no_cuda=False,
        seed=42,
        fp16=False,
        fp16_opt_level="O1",
        local_rank=-1,
    )
    # Convert dict to objects
    model_args = DictToObj(model_args)
    data_args = DictToObj(data_args)

    # Logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if training_args.local_rank in [-1, 0] else logging.WARN,
    )
    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        training_args.local_rank,
        training_args.device,
        training_args.n_gpu,
        bool(training_args.local_rank != -1),
        training_args.fp16,
    )
    logger.info("Training/evaluation parameters %s", training_args)

    # Seed
    set_seed(training_args.seed)

    # Load tokenizer and model
    config = AutoConfig.from_pretrained(
        model_args.model_name_or_path, cache_dir=model_args.cache_dir
    )
    tokenizer = AutoTokenizer.from_pretrained(
        model_args.model_name_or_path, cache_dir=model_args.cache_dir
    )
    model = AutoModelWithLMHead.from_pretrained(
        model_args.model_name_or_path,
        from_tf=bool(".ckpt" in model_args.model_name_or_path),
        config=config,
        cache_dir=model_args.cache_dir,
    )

    # Add special tokens
    tokenizer.add_special_tokens({"sep_token": sep})
    model.resize_token_embeddings(len(tokenizer))

    # Load dataset
    train_dataset = (
        get_dataset(data_args, tokenizer=tokenizer) if training_args.do_train else None
    )

    eval_dataset = (
        get_dataset(data_args, tokenizer=tokenizer, evaluate=True)
        if training_args.do_eval
        else None
    )

    data_collator = DataCollatorForLanguageModeling(
        tokenizer=tokenizer,
        mlm=data_args.mlm,
        mlm_probability=data_args.mlm_probability,
    )

    # Initialize trainer
    trainer = Trainer(
        model=model,
        args=training_args,
        data_collator=data_collator,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset,
        prediction_loss_only=True,
    )

    model_path = (
        model_args.model_name_or_path
        if model_args.model_name_or_path is not None
        and os.path.isdir(model_args.model_name_or_path)
        else None
    )

    # Train the model
    start = timer()
    train_results = trainer.train(model_path=model_path)
    end = timer()
    trainer.save_model()
    if trainer.is_world_master():
        tokenizer.save_pretrained(training_args.output_dir)

    # Calculate training time
    print(f"Training took {(end - start) / 3600} hours.")

    # Evaluate model
    results = {}
    logger.info("*** Evaluate ***")
    eval_output = trainer.evaluate()
    perplexity = math.exp(eval_output["eval_loss"])
    result = {"perplexity": perplexity}
    output_eval_file = os.path.join(training_args.output_dir, "eval_results_lm.txt")

    # Evaluation output
    with open(output_eval_file, "w") as writer:
        logger.info("***** Eval results *****")
        for key in sorted(result.keys()):
            logger.info("  %s = %s", key, str(result[key]))
            writer.write("%s = %s\n" % (key, str(result[key])))

    # Log evaluation and result (ppl)
    results.update(result)
    wandb.log({"train_loss": train_results.training_loss})
    wandb.log({"eval_loss": eval_output["eval_loss"]})
    wandb.log(result)
    wandb.log({"train_time": (end - start) / 3600})


if __name__ == "__main__":
    main()
