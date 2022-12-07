#!/usr/bin/env bash

if [[ "$1" == "TEMPONet" ]]; then
    echo TEMPONet testing
    python3 -u main.py --network TEMPONet
else
    echo ViT testing
    python3 -u main.py --network $1 --tcn_layers $2 --blocks $3 --dim_head $4 --heads $5 --depth $6 --patch_size1 $7 --patch_size2 $8 --patch_size3 $9 --ch_1 ${10} --ch_2 ${11} --ch_3 ${12} | tee $HOME/Alessio/bioformer/log/my-bioformer-$1-$2-$3-$4-$5-$6-$7-${10}.txt
fi