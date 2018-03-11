from __future__ import absolute_import
import os
import sys
import time
import datetime
import argparse
import os.path as osp
import numpy as np

import torch
import torch.nn as nn
import torch.backends.cudnn as cudnn
from torch.utils.data import DataLoader
from torch.autograd import Variable

import data_manager
from dataset_loader import ImageDataset
import transforms as T
import models
from losses import CrossEntropyLabelSmooth
from utils import AverageMeter, Logger
from eval_metrics import evaluate

parser = argparse.ArgumentParser(description='Train image model with cross entropy loss')
# Datasets
parser.add_argument('-d', '--dataset', type=str, default='market1501',
                    choices=data_manager.get_names())
parser.add_argument('-j', '--workers', default=4, type=int,
                    help="number of data loading workers (default: 4)")
parser.add_argument('--height', type=int, default=256,
                    help="height of an image (default: 256)")
parser.add_argument('--width', type=int, default=128,
                    help="width of an image (default: 128)")
# Optimization options
parser.add_argument('--max-epoch', default=10, type=int,
                    help="maximum epochs to run")
parser.add_argument('--start-epoch', default=0, type=int,
                    help="manual epoch number (useful on restarts)")
parser.add_argument('--train-batch', default=32, type=int,
                    help="train batch size")
parser.add_argument('--test-batch', default=100, type=int, help="test batch size")
parser.add_argument('--lr', '--learning-rate', default=3e-04, type=float,
                    help="initial learning rate")
parser.add_argument('--weight-decay', '--wd', default=5e-04, type=float,
                    help="weight decay (default: 5e-04)")
# Architecture
parser.add_argument('-a', '--arch', type=str, default='resnet50', choices=models.get_names())
# Miscs
parser.add_argument('--print-freq', type=int, default=5, help="print frequency")
parser.add_argument('--seed', type=int, default=1, help="manual seed")
parser.add_argument('--resume', type=str, default='', metavar='PATH')
parser.add_argument('--evaluate', action='store_true', help="evaluation only")
parser.add_argument('--eval-step', type=int, default=50, help="run evaluation for every N epochs")
parser.add_argument('--save-dir', type=str, default='log')
parser.add_argument('--use-cpu', action='store_true', help="use cpu")
parser.add_argument('--gpu-devices', default='0', type=str, help='gpu device ids for CUDA_VISIBLE_DEVICES')

args = parser.parse_args()

def main():
    torch.manual_seed(args.seed)
    os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_devices
    use_gpu = torch.cuda.is_available()
    if args.use_cpu: use_gpu = False

    if not args.evaluate:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_train.txt'))
    else:
        sys.stdout = Logger(osp.join(args.save_dir, 'log_test.txt'))
    print("==========\nArgs:{}\n==========".format(args))

    if use_gpu:
        print("Currently using GPU")
        cudnn.benchmark = True
        torch.cuda.manual_seed_all(args.seed)
    else:
        print("Currently using CPU (GPU is highly recommended)")

    print("Initializing dataset {}".format(args.dataset))
    dataset = data_manager.init_dataset(name=args.dataset)

    transform_train = T.Compose([
        T.Random2DTranslation(args.height, args.width),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    transform_test = T.Compose([
        T.Resize((args.height, args.width)),
        T.ToTensor(),
        T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ])

    trainloader = DataLoader(
        ImageDataset(dataset.train, transform=transform_train),
        batch_size=args.train_batch, shuffle=True, num_workers=args.workers,
        pin_memory=False, drop_last=True,
    )

    queryloader = DataLoader(
        ImageDataset(dataset.query, transform=transform_test),
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=False, drop_last=False,
    )

    galleryloader = DataLoader(
        ImageDataset(dataset.gallery, transform=transform_test),
        batch_size=args.test_batch, shuffle=False, num_workers=args.workers,
        pin_memory=False, drop_last=False,
    )

    print("Initializing model: {}".format(args.arch))
    model = models.init_model(name=args.arch, num_classes=dataset.num_train_pids)
    print("Model size: {:.5f}M".format(sum(p.numel() for p in model.parameters())/1000000.0))

    criterion = CrossEntropyLabelSmooth(num_classes=dataset.num_train_pids, use_gpu=use_gpu)
    optimizer = torch.optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    start_epoch = args.start_epoch

    if args.resume:
        print("Loading checkpoint from {}".format(args.resume))
        checkpoint = torch.load(args.resume)
        model.load_state_dict(checkpoint['state_dict'])
        start_epoch = checkpoint['epoch']

    if use_gpu:
        model = nn.DataParallel(model).cuda()

    if args.evaluate:
        print("Evaluate only")
        start_time = time.time()
        test(model, queryloader, galleryloader, use_gpu)
        elapsed = time.time() - start_time
        elapsed = str(datetime.timedelta(seconds=elapsed))
        print("Finished. Total elapsed time: {}".format(elapsed))
        return

    start_time = time.time()
    best_rank1 = -np.inf

    for epoch in range(start_epoch, args.max_epoch):
        print("==> Epoch {}/{}".format(epoch+1, args.max_epoch))
        train(model, criterion, optimizer, trainloader, use_gpu)
        if (epoch+1) % args.eval_step == 0 or (epoch+1) == args.max_epoch:
            print("==> Test")
            rank1 = test(model, queryloader, galleryloader, use_gpu)
            is_best = rank1 > best_rank1
            if is_best: best_rank1 = rank1

            save_checkpoint({
                'state_dict': model.state_dict(),
                'rank1': rank1,
                'epoch': epoch,
            }, is_best, osp.join(args.save_dir, 'checkpoint_ep' + str(epoch+1) + '.pth.tar'))

    elapsed = time.time() - start_time
    elapsed = str(datetime.timedelta(seconds=elapsed))
    print("Finished. Total elapsed time: {}".format(elapsed))

def train(model, criterion, optimizer, trainloader, use_gpu):
    model.train()
    losses = AverageMeter()

    for batch_idx, (imgs, pids, _) in enumerate(trainloader):
        if use_gpu:
            imgs, pids = imgs.cuda(), pids.cuda()
        imgs, pids = Variable(imgs), Variable(pids)
        outputs = model(imgs)
        loss = criterion(outputs, pids)
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()
        losses.update(loss.data[0], pids.size(0))

        if (batch_idx+1) % args.print_freq == 0:
            print("Batch {}/{}\t Loss {:.6f} ({:.6f})".format(batch_idx+1, len(trainloader), losses.val, losses.avg))

def test(model, queryloader, galleryloader, use_gpu):
    model.eval()
    qf = [] # query features
    gf = [] # gallery features

    for batch_idx, (imgs, _, _) in enumerate(queryloader):
        if use_gpu:
            imgs = imgs.cuda()
        imgs = Variable(imgs)
        features = model(imgs)
        features = features.data.cpu()
        qf.append(features)
    qf = torch.cat(qf, 0)

    print("Extracted features for query set, obtained {}-by-{} matrix".format(qf.size(0), qf.size(1)))

    for batch_idx, (imgs, _, _) in enumerate(galleryloader):
        if use_gpu:
            imgs = imgs.cuda()
        imgs = Variable(imgs)
        features = model(imgs)
        features = features.data.cpu()
        gf.append(features)
    gf = torch.cat(gf, 0)

    print("Extracted features for gallery set, obtained {}-by-{} matrix".format(gf.size(0), gf.size(1)))

    print("Computing distance matrix")

    m, n = qf.size(0), gf.size(0)
    distmat = torch.pow(qf, 2).sum(dim=1, keepdim=True).expand(m, n) + \
              torch.pow(gf, 2).sum(dim=1, keepdim=True).expand(n, m).t()
    distmat.addmm_(1, -2, qf, gf.t())
    distmat = distmat.numpy()

    import h5py
    h5file = h5py.File('data/features.h5', 'w')
    h5file.create_dataset('distmat', data=distmat)
    h5file.close()
    
    q_pids, q_camids = [], []
    for _, pids, camids in queryloader:
        q_pids.extend(pids)
        q_camids.extend(camids)
    q_pids = np.asarray(q_pids)
    q_camids = np.asarray(q_camids)

    g_pids, g_camids = [], []
    for _, pids, camids in galleryloader:
        g_pids.extend(pids)
        g_camids.extend(camids)
    g_pids = np.asarray(g_pids)
    g_camids = np.asarray(g_camids)

    print("Computing CMC and mAP")
    cmc, mAP = evaluate(distmat, q_pids, g_pids, q_camids, g_camids)

    print("Results: CMC Rank-1/5/10/20 {:.1%}/{:.1%}/{:.1%}/{:.1%}\t mAP {:.1%}" \
          .format(cmc[0], cmc[4], cmc[9], cmc[19], mAP))

    return cmc[0]

if __name__ == '__main__':
    main()







