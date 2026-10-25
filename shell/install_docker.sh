#!/bin/bash

# 脚本功能：自动在 CentOS 7 上安装 Docker CE
# 版本：1.0
# 更新时间：2026-10-23

# 使用阿里云镜像源安装 Docker CE
# 1. 清理之前的残留文件
rm -f /etc/yum.repos.d/docker-ce.repo

# 2. 下载并添加阿里云 Docker CE 镜像源
curl -fsSL http://mirrors.aliyun.com/docker-ce/linux/centos/docker-ce.repo \
    -o /etc/yum.repos.d/docker-ce.repo

# 3. 刷新 yum 缓存
yum makecache fast

# 4. 安装 Docker CE
yum install -y docker-ce docker-ce-cli containerd.io

# 5. 启动并设置开机自启
systemctl start docker
systemctl enable docker

# 6. 验证安装
docker --version
