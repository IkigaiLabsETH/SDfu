# reworked from 
# https://github.com/huggingface/diffusers/blob/main/examples/textual_inversion/textual_inversion.py
# https://github.com/adobe-research/custom-diffusion

import os
import sys
import math
import random
import argparse
import itertools

import torch
import torch.nn.functional as F

import transformers
transformers.utils.logging.set_verbosity_warning()
from transformers import CLIPTextModel, CLIPTokenizer
from diffusers import StableDiffusionPipeline, AutoencoderKL, UNet2DConditionModel, DDIMScheduler

from util.finetune import FinetuneDataset, custom_diff, save_delta, load_delta, save_embeds
from util.utils import save_img, save_cfg, progbar

import warnings
warnings.filterwarnings("ignore")

parser = argparse.ArgumentParser()
# inputs
parser.add_argument("--type",           default="custom", choices=['text','custom'], help="Textual Inversion or Custom Diffusion?")
parser.add_argument('-t', "--token",    default=None, help="special word(s) to invoke the embedding, separated by '+'")
parser.add_argument("--term",           default=None, help="generic word(s), associating with that object or style, separated by '+'")
parser.add_argument("--data",           default=None, help="folder containing target images")
parser.add_argument("--term_data",      default=None, help="folder containing generic class images (priors for new token)")
parser.add_argument('-st', "--style",   action='store_true', help="True = style, False = object")
parser.add_argument('-m',  '--model',   default='15', choices=['12','14','15','21','21v'])
parser.add_argument('-md', '--maindir', default='./models', help='Main SD models directory')
parser.add_argument('-r', "--delta_ckpt", default=None, help="path to the delta checkpoint to resume from")
parser.add_argument('-o', "--out_dir",  default="train", help="Output directory")
# train
parser.add_argument('-b','--batch_size', default=1, type=int, help="batch size for training dataloader")
parser.add_argument('-ts', "--train_steps", default=2000, type=int, help="Number of training steps")
parser.add_argument("--save_step",      default=500, type=int, help="how often to save models and samples")
parser.add_argument('-lo', "--low_mem", action="store_true", help="Use gradient checkpointing: less memory, slower training")
parser.add_argument("--freeze_model",   default='crossattn_kv', help="set 'crossattn' to enable fine-tuning of all key, value, query matrices")
parser.add_argument('-lr', "--lr",      default=1e-5, type=float, help="Initial learning rate") # 1e-3 ~ 5e-4 for text inv
parser.add_argument("--scale_lr",       default=True, help="Scale learning rate by batch")
parser.add_argument('-S',  '--seed',    default=None, type=int, help="A seed for reproducible training.")
a = parser.parse_args()

device = torch.device('cuda')

if a.seed is not None:
    from pytorch_lightning import seed_everything
    seed_everything(a.seed)

def main():
    os.makedirs(a.out_dir, exist_ok=True)
    save_cfg(a, a.out_dir)

    # paths
    subdir = 'v2v' if a.model=='21v' else 'v2' if a.model[0]=='2' else 'v1'
    txtenc_path = os.path.join(a.maindir, subdir, 'text')
    sched_path = os.path.join(a.maindir, subdir, 'scheduler_config.json')
    unet_path = os.path.join(a.maindir, subdir, 'unet' + a.model)
    vae_path = os.path.join(a.maindir, subdir, 'vae')

    # load models
    text_encoder = CLIPTextModel.from_pretrained(txtenc_path, torch_dtype=torch.float16).to(device)
    tokenizer    = CLIPTokenizer.from_pretrained(txtenc_path, torch_dtype=torch.float16)
    unet  = UNet2DConditionModel.from_pretrained(unet_path,   torch_dtype=torch.float16).to(device)
    vae          = AutoencoderKL.from_pretrained(vae_path,    torch_dtype=torch.float16).to(device)
    scheduler    = DDIMScheduler.from_pretrained(sched_path)
    resolution = unet.config.sample_size * 2 ** (len(vae.config.block_out_channels) - 1)

    if a.type=='custom' and a.delta_ckpt is not None and os.path.isfile(a.delta_ckpt):
        load_delta(a.delta_ckpt, text_encoder, tokenizer, unet, freeze_model=a.freeze_model)

    # parse inputs
    with_prior = a.term_data is not None and a.term is not None # Use generic terms as priors
    mod_tokens  = ['<%s>' % t for t in a.token.split('+')]
    init_tokens = a.term.split('+')
    data_dirs   = a.data.split('+')
    assert len(mod_tokens) == len(init_tokens) == len(data_dirs), "Provide equal num of tokens, terms and data folders (separated by '+')"
    if with_prior:
        term_data_dirs = a.term_data.split('+')
        assert len(mod_tokens) == len(term_data_dirs), "Provide equal num of tokens, terms and data folders (separated by '+')"

    # prepare tokens
    mod_tokens_id  = []
    init_tokens_id = []
    for mod_token, init_token in zip(mod_tokens, init_tokens):
        num_added_tokens = tokenizer.add_tokens(mod_token)
        mod_tokens_id.append(tokenizer.convert_tokens_to_ids(mod_token)) # non-existing token
        init_tokens_id.append(tokenizer.encode(init_token, add_special_tokens=False)[0]) # existing token
    print(' tokens :: mod', mod_tokens, mod_tokens_id, '.. init', init_tokens, init_tokens_id, '.. with prior' if with_prior else "")
    # Init new token(s) with the given initial token(s)
    text_encoder.resize_token_embeddings(len(tokenizer)) # new token in tokenizer => resize token embeddings
    token_embeds = text_encoder.get_input_embeddings().weight.data
    for (x,y) in zip(mod_tokens_id, init_tokens_id):
        token_embeds[x] = token_embeds[y]

    # data
    inputs = [{"caption": '%s %s' % (mod_tokens[i], init_tokens[i]), "term": init_tokens[i], "data": data_dirs[i]} for i in range(len(mod_tokens))]
    if with_prior:
        inputs = [{**inputs[i], "term_data": term_data_dirs[i]} for i in range(len(mod_tokens))]
    train_dataset = FinetuneDataset(inputs, tokenizer, resolution, style=a.style, augment=True, flip=True)
    def collate_fn(examples):
        input_ids = [example["instance_token_id"] for example in examples]
        images    = [example["instance_image"]    for example in examples]
        if with_prior:
            input_ids += [example["class_token_id"] for example in examples]
            images    += [example["class_image"]    for example in examples]
        images = torch.stack(images).to(memory_format=torch.contiguous_format).float()
        input_ids = tokenizer.pad({"input_ids": input_ids}, padding="max_length", max_length=tokenizer.model_max_length, return_tensors="pt").input_ids
        return {"input_ids": input_ids.to(device), "images": images.to(device)}
    train_dataloader = torch.utils.data.DataLoader(train_dataset, batch_size=a.batch_size, shuffle=True, collate_fn=collate_fn)

    # optimization
    if a.type == 'text': # only new embedding(s)
        params_to_optimize = text_encoder.get_input_embeddings().parameters()
    elif a.freeze_model == 'crossattn': # custom: embeddings & unet attention all
        params_to_optimize = itertools.chain(text_encoder.get_input_embeddings().parameters(), 
                                             [x[1] for x in unet.named_parameters() if 'attn2' in x[0]])
    else: # custom: embeddings & unet attention k,v
        params_to_optimize = itertools.chain(text_encoder.get_input_embeddings().parameters(), 
                                             [x[1] for x in unet.named_parameters() if 'attn2.to_k' in x[0] or 'attn2.to_v' in x[0]])

    optimizer = torch.optim.AdamW(params_to_optimize, lr=a.lr, betas=(0.9, 0.999), weight_decay=0.01, eps=1e-08)

    if a.low_mem:
        unet.enable_gradient_checkpointing()
        text_encoder.gradient_checkpointing_enable()

    if a.scale_lr:
        a.lr *= a.batch_size
        if with_prior:
            a.lr *= 2.

    # cast modules :: inference = fp16 freezed, training = fp32 with grad
    weight_dtype = torch.float16 # torch.float16 torch.bfloat16
    vae.to(dtype=weight_dtype).requires_grad_(False)
    text_encoder.text_model.encoder.requires_grad_(False)
    text_encoder.text_model.final_layer_norm.requires_grad_(False)
    text_encoder.text_model.embeddings.position_embedding.requires_grad_(False)
    if a.type=='text':
        unet.requires_grad_(False)
        unet_dtype = weight_dtype
    else:
        unet = custom_diff(unet, a.freeze_model)
        unet_dtype = torch.float32 # !!! must be trained as float32; otherwise inf/nan
        unet0 = UNet2DConditionModel.from_pretrained(unet_path, torch_dtype=torch.float16) # required to compress delta
    unet.to(dtype=unet_dtype)
    text_encoder.to(dtype=torch.float32) # !!! must be trained as float32; otherwise inf/nan

    # unet.enable_xformers_memory_efficient_attention() # does not work here yet

    # training loop
    epoch_steps = len(train_dataloader)
    num_epochs  = math.ceil(a.train_steps / epoch_steps)
    global_step = 0
    print(f" batch {a.batch_size}, data count {len(train_dataset)}")
    print(f" steps {a.train_steps}, epochs {num_epochs}")
    pbar = progbar(a.train_steps)
    for epoch in range(num_epochs):
        if a.type == 'custom': unet.train()
        text_encoder.train()
        for step, batch in enumerate(train_dataloader):
            latents = vae.encode(batch["images"].to(dtype=weight_dtype)).latent_dist.sample().detach()
            latents = latents * vae.config.scaling_factor # 0.18215
            noise = torch.randn_like(latents)
            bsize = latents.shape[0]
            timesteps = torch.randint(0, scheduler.config.num_train_timesteps, (bsize,), device=latents.device)
            timesteps = timesteps.long()
            target = noise if scheduler.config.prediction_type == "epsilon" else scheduler.get_velocity(latents, noise, timesteps) # v_prediction

            noisy_latents = scheduler.add_noise(latents, noise, timesteps).to(dtype=unet_dtype) # Add noise to latents (forward diffusion process)
            text_cond = text_encoder(batch["input_ids"])[0].to(dtype=unet_dtype)
            model_pred = unet(noisy_latents, timesteps, text_cond).sample # Predict the noise residual
                
            if with_prior:
                model_pred, model_pred_prior = model_pred.float().chunk(2)
                target,     target_prior     = target.float().detach().chunk(2)
                loss  = F.mse_loss(model_pred,       target,       reduction="mean") # instance loss
                loss += F.mse_loss(model_pred_prior, target_prior, reduction="mean") # prior loss
            else:
                loss  = F.mse_loss(model_pred.float(), target.float().detach(), reduction="mean")

            loss.backward()

            # ensure we don't update any other embeddings except new token
            grads_text_encoder = text_encoder.get_input_embeddings().weight.grad
            index_no_updates = torch.arange(len(tokenizer)) != mod_tokens_id[0]
            for i in range(len(mod_tokens_id[1:])):
                index_no_updates = index_no_updates & (torch.arange(len(tokenizer)) != mod_tokens_id[i])
            grads_text_encoder.data[index_no_updates, :] = grads_text_encoder.data[index_no_updates, :].fill_(0)

            optimizer.step()
            optimizer.zero_grad()

            global_step += 1
            if global_step % a.save_step == 0:
                if a.type == 'text':
                    save_path = os.path.join(a.out_dir, '%s-%04d.pt' % (a.token, global_step))
                    save_embeds(save_path, text_encoder, mod_tokens, mod_tokens_id)
                else:
                    save_path = os.path.join(a.out_dir, '%s-%04d.ckpt' % (a.token, global_step))
                    save_delta(save_path, text_encoder, unet, mod_tokens, mod_tokens_id, a.freeze_model, unet0=unet0)

                # test sample
                pipetest = StableDiffusionPipeline(vae, text_encoder, tokenizer, unet, scheduler, None, None, False).to(device)
                pipetest.set_progress_bar_config(disable=True)
                generator = None if a.seed is None else torch.Generator(device=device).manual_seed(a.seed)
                with torch.autocast("cuda"):
                    for i, (mod_token, init_token) in enumerate(zip(mod_tokens, init_tokens)):
                        prompt = 'photo of %s %s' % (mod_token, init_token)
                        image = pipetest(prompt, num_inference_steps=50, generator=generator).images[0]
                        image.save(os.path.join(a.out_dir, '%s-%04d.jpg' % (mod_token[1:-1], global_step)))

            pbar.upd("loss %.4g" % loss.detach().item())

            if global_step >= a.train_steps:
                break

    if a.type == 'text':
        save_path = os.path.join(a.out_dir, '%s.pt' % a.token)
        save_embeds(save_path, text_encoder, mod_tokens, mod_tokens_id)
    else:
        save_path = os.path.join(a.out_dir, '%s.ckpt' % a.token)
        save_delta(save_path, text_encoder, unet, mod_tokens, mod_tokens_id, a.freeze_model)


if __name__ == "__main__":
    main()