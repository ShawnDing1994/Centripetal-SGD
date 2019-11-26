from torch.utils.tensorboard import SummaryWriter
from base_config import BaseConfigByEpoch
from model_map import get_model_fn
from dataset import create_dataset, load_cuda_data, num_iters_per_epoch
from torch.nn.modules.loss import CrossEntropyLoss
from utils.engine import Engine
from utils.pyt_utils import ensure_dir
from utils.misc import torch_accuracy, AvgMeter
from collections import OrderedDict
import torch
from tqdm import tqdm
import time
from builder import ConvBuilder
from utils.lr_scheduler import get_lr_scheduler
import os
from ding_test import run_eval

TRAIN_SPEED_START = 0.1
TRAIN_SPEED_END = 0.2

COLLECT_TRAIN_LOSS_EPOCHS = 10

# try:
#     from apex.parallel.distributed import DistributedDataParallel
#     from apex import amp
# except ImportError:
#     raise ImportError('Use APEX for multi-precision via apex.amp')

def train_one_step(net, data, label, optimizer, criterion, if_accum_grad = False, gradient_mask_tensor = None):
    pred = net(data)
    loss = criterion(pred, label)
    loss.backward()
    if not if_accum_grad:
        if gradient_mask_tensor is not None:
            for name, param in net.named_parameters():
                if name in gradient_mask_tensor:
                    param.grad = param.grad * gradient_mask_tensor[name]
        optimizer.step()
        optimizer.zero_grad()
    acc, acc5 = torch_accuracy(pred, label, (1,5))
    return acc, acc5, loss


def sgd_optimizer(cfg, model, no_l2_keywords, use_nesterov):
    params = []
    for key, value in model.named_parameters():
        if not value.requires_grad:
            continue
        lr = cfg.base_lr
        weight_decay = cfg.weight_decay
        if "bias" in key or "bn" in key or "BN" in key:
            # lr = cfg.SOLVER.BASE_LR * cfg.SOLVER.BIAS_LR_FACTOR
            weight_decay = cfg.weight_decay_bias
            print('set weight_decay_bias={} for {}'.format(weight_decay, key))
        for kw in no_l2_keywords:
            if kw in key:
                weight_decay = 0
                print('NOTICE! weight decay = 0 for ', key)
        if 'bias' in key:
            apply_lr = 2 * lr
        else:
            apply_lr = lr
        params += [{"params": [value], "lr": apply_lr, "weight_decay": weight_decay}]
    # optimizer = torch.optim.Adam(params, lr)
    optimizer = torch.optim.SGD(params, lr, momentum=cfg.momentum, nesterov=use_nesterov)
    return optimizer

def get_optimizer(cfg, model, no_l2_keywords, use_nesterov=False):
    return sgd_optimizer(cfg, model, no_l2_keywords, use_nesterov=use_nesterov)

def get_criterion(cfg):
    return CrossEntropyLoss()


def ding_train(cfg:BaseConfigByEpoch, net=None, train_dataloader=None, val_dataloader=None, show_variables=False, convbuilder=None, beginning_msg=None,
               init_hdf5=None, no_l2_keywords=None, gradient_mask=None, use_nesterov=False, tensorflow_style_init=False):

    # LOCAL_RANK = 0
    #
    # num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1
    # is_distributed = num_gpus > 1
    #
    # if is_distributed:
    #     torch.cuda.set_device(LOCAL_RANK)
    #     torch.distributed.init_process_group(
    #         backend="nccl", init_method="env://"
    #     )
    #     synchronize()
    #
    # torch.backends.cudnn.benchmark = True

    ensure_dir(cfg.output_dir)
    ensure_dir(cfg.tb_dir)
    with Engine() as engine:

        is_main_process = (engine.world_rank == 0) #TODO correct?

        logger = engine.setup_log(
            name='train', log_dir=cfg.output_dir, file_name='log.txt')

        # -- typical model components model, opt,  scheduler,  dataloder --#
        if net is None:
            net = get_model_fn(cfg.dataset_name, cfg.network_type)

        if convbuilder is None:
            convbuilder = ConvBuilder(base_config=cfg)

        model = net(cfg, convbuilder).cuda()

        if train_dataloader is None:
            train_dataloader = create_dataset(cfg.dataset_name, cfg.dataset_subset, cfg.global_batch_size)
        if cfg.val_epoch_period > 0 and val_dataloader is None:
            val_dataloader = create_dataset(cfg.dataset_name, 'val', batch_size=100)    #TODO 100?

        print('NOTE: Data prepared')
        print('NOTE: We have global_batch_size={} on {} GPUs, the allocated GPU memory is {}'.format(cfg.global_batch_size, torch.cuda.device_count(), torch.cuda.memory_allocated()))

        if no_l2_keywords is None:
            no_l2_keywords = []

        optimizer = get_optimizer(cfg, model, no_l2_keywords=no_l2_keywords, use_nesterov=use_nesterov)
        scheduler = get_lr_scheduler(cfg, optimizer)
        criterion = get_criterion(cfg).cuda()

        # model, optimizer = amp.initialize(model, optimizer, opt_level="O0")

        engine.register_state(
            scheduler=scheduler, model=model, optimizer=optimizer, cfg=cfg)

        if engine.distributed:
            print('Distributed training, engine.world_rank={}'.format(engine.world_rank))
            model = torch.nn.parallel.DistributedDataParallel(
                model, device_ids=[engine.world_rank],
                broadcast_buffers=False, )
            # model = DistributedDataParallel(model, delay_allreduce=True)
        elif torch.cuda.device_count() > 1:
            print('Single machine multiple GPU training')
            model = torch.nn.parallel.DataParallel(model)

        if tensorflow_style_init:
            for k, v in model.named_parameters():
                if v.dim() in [2, 4]:
                    torch.nn.init.xavier_uniform_(v)
                    print('init {} as xavier_uniform'.format(k))
                if 'bias' in k and 'bn' not in k.lower():
                    torch.nn.init.zeros_(v)
                    print('init {} as zero'.format(k))


        if cfg.init_weights:
            engine.load_checkpoint(cfg.init_weights)

        if init_hdf5:
            engine.load_hdf5(init_hdf5)

        if show_variables:
            engine.show_variables()

        # ------------ do training ---------------------------- #
        if beginning_msg:
            engine.log(beginning_msg)
        logger.info("\n\nStart training with pytorch version {}".format(torch.__version__))

        iteration = engine.state.iteration
        # done_epochs = iteration // num_train_examples_per_epoch(cfg.dataset_name)
        iters_per_epoch = num_iters_per_epoch(cfg)
        max_iters = iters_per_epoch * cfg.max_epochs
        tb_writer = SummaryWriter(cfg.tb_dir)
        tb_tags = ['Top1-Acc', 'Top5-Acc', 'Loss']

        model.train()

        done_epochs = iteration // iters_per_epoch

        engine.save_hdf5(os.path.join(cfg.output_dir, 'init.hdf5'))

        recorded_train_time = 0
        recorded_train_examples = 0

        collected_train_loss_sum = 0
        collected_train_loss_count = 0

        if gradient_mask is not None:
            gradient_mask_tensor = {}
            for name, value in gradient_mask.items():
                gradient_mask_tensor[name] = torch.Tensor(value).cuda()
        else:
            gradient_mask_tensor = None

        for epoch in range(done_epochs, cfg.max_epochs):

            pbar = tqdm(range(iters_per_epoch))
            top1 = AvgMeter()
            top5 = AvgMeter()
            losses = AvgMeter()
            discrip_str = 'Epoch-{}/{}'.format(epoch, cfg.max_epochs)
            pbar.set_description('Train' + discrip_str)


            if cfg.val_epoch_period > 0 and epoch % cfg.val_epoch_period == 0:
                model.eval()
                val_iters = 500 if cfg.dataset_name == 'imagenet' else 100  # use batch_size=100 for val on ImagenNet and CIFAR
                eval_dict, _ = run_eval(val_dataloader, val_iters, model, criterion, discrip_str, dataset_name=cfg.dataset_name)
                val_top1_value = eval_dict['top1'].item()
                val_top5_value = eval_dict['top5'].item()
                val_loss_value = eval_dict['loss'].item()
                for tag, value in zip(tb_tags, [val_top1_value, val_top5_value, val_loss_value]):
                    tb_writer.add_scalars(tag, {'Val': value}, iteration)
                engine.log('validate at epoch {}, top1={:.5f}, top5={:.5f}, loss={:.6f}'.format(epoch, val_top1_value, val_top5_value, val_loss_value))
                model.train()

            for _ in pbar:

                start_time = time.time()
                data, label = load_cuda_data(train_dataloader, cfg.dataset_name)
                data_time = time.time() - start_time

                if_accum_grad = ((iteration % cfg.grad_accum_iters) != 0)

                train_net_time_start = time.time()
                acc, acc5, loss = train_one_step(model, data, label, optimizer, criterion, if_accum_grad, gradient_mask_tensor=gradient_mask_tensor)
                train_net_time_end = time.time()

                if iteration > TRAIN_SPEED_START * max_iters and iteration < TRAIN_SPEED_END * max_iters:
                    recorded_train_examples += cfg.global_batch_size
                    recorded_train_time += train_net_time_end - train_net_time_start

                scheduler.step()

                if iteration % cfg.tb_iter_period == 0 and is_main_process:
                    for tag, value in zip(tb_tags, [acc.item(), acc5.item(), loss.item()]):
                        tb_writer.add_scalars(tag, {'Train': value}, iteration)


                top1.update(acc.item())
                top5.update(acc5.item())
                losses.update(loss.item())

                if epoch >= cfg.max_epochs - COLLECT_TRAIN_LOSS_EPOCHS:
                    collected_train_loss_sum += loss.item()
                    collected_train_loss_count += 1

                pbar_dic = OrderedDict()
                pbar_dic['data-time'] = '{:.2f}'.format(data_time)
                pbar_dic['cur_iter'] = iteration
                pbar_dic['lr'] = scheduler.get_lr()[0]
                pbar_dic['top1'] = '{:.5f}'.format(top1.mean)
                pbar_dic['top5'] = '{:.5f}'.format(top5.mean)
                pbar_dic['loss'] = '{:.5f}'.format(losses.mean)
                pbar.set_postfix(pbar_dic)

                if iteration >= max_iters or iteration % cfg.ckpt_iter_period == 0:
                    engine.update_iteration(iteration)
                    if (not engine.distributed) or (engine.distributed and is_main_process):
                        engine.save_and_link_checkpoint(cfg.output_dir)

                iteration += 1
                if iteration >= max_iters:
                    break

            #   do something after an epoch?
            if iteration >= max_iters:
                break
        #   do something after the training
        if recorded_train_time > 0:
            exp_per_sec = recorded_train_examples / recorded_train_time
        else:
            exp_per_sec = 0
        engine.log(
            'TRAIN speed: from {} to {} iterations, batch_size={}, examples={}, total_net_time={:.4f}, examples/sec={}'
            .format(int(TRAIN_SPEED_START * max_iters), int(TRAIN_SPEED_END * max_iters), cfg.global_batch_size,
                    recorded_train_examples, recorded_train_time, exp_per_sec))
        if cfg.save_weights:
            engine.save_checkpoint(cfg.save_weights)
            print('NOTE: training finished, saved to {}'.format(cfg.save_weights))
        engine.save_hdf5(os.path.join(cfg.output_dir, 'finish.hdf5'))
        engine.log('TRAIN LOSS collected over last {} epochs: {:.6f}'.format(COLLECT_TRAIN_LOSS_EPOCHS,
                                                                             collected_train_loss_sum / collected_train_loss_count))






            # if engine.world_rank == 0:
            #     if iteration % 20 == 0 or iteration == max_iter:
            #         # loss_dict = reducke_loss_dict(loss_dict)
            #         log_str = 'it:%d, lr:%.1e, ' % (
            #             iteration, optimizer.param_groups[0]["lr"])
            #         for key in loss_dict:
            #             tb_writer.add_scalar(
            #                 key, loss_dict[key].mean(), global_step=iteration)
            #             log_str += key + ': %.3f, ' % float(loss_dict[key])
            #         logger.info(log_str)