#!/bin/bash

# Copyright
# 2024, Johns Hopkins University (Author: Prabhav Singh)
# Apache 2.0.

#SBATCH --job-name=ActiveLearner
#SBATCH --nodes=1
#SBATCH --gpus=1
#SBATCH --mem-per-cpu=16G
#SBATCH --partition=gpu
#SBATCH --mail-user="psingh54@jhu.edu"

source /home/psingh54/.bashrc
module load cuda/12.1

conda activate llm_rubric_env

python /export/fs06/psingh54/ActiveRubric-Internal/src/activeLearner.py --examples_per_cycle 50 --features_per_example 5 \
    --experiment all --loss_type l2 --resample_validation --run_until_exhausted --dataset hanna --runner prabhav \
    --use_embedding True