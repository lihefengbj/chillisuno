#!/bin/bash

#===============================================================================
# Docker 安装 PHP 8.2.35 FPM 脚本（国内优化版）
# 目标: 安装完成后可通过 Nginx fastcgi_pass 127.0.0.1:9000 访问
# 用法: sudo ./install-php82.sh
#===============================================================================

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log_info()  { echo -e "${BLUE}[INFO]${NC} $1"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_err()   { echo -e "${RED}[ERROR]${NC} $1"; }

# 配置变量
PHP_VERSION="8.2.35"
CONTAINER_NAME="php82-fpm"
PHP_PORT=9000
INSTALL_DIR="${HOME}/php82-docker"
WEB_ROOT="${INSTALL_DIR}/www"
APP_ROOT="/data0/www/kfcm_server"

# 国内镜像加速器列表
MIRRORS=(
    "https://docker.mirrors.ustc.edu.cn"
    "https://hub-mirror.c.163.com"
    "https://mirror.baidubce.com"
    "https://docker.m.daocloud.io"
)

#===============================================================================
# 前置检查
#===============================================================================

check_requirements() {
    log_info "检查系统环境..."
    
    if ! command -v docker &> /dev/null; then
        log_err "Docker 未安装，请先安装 Docker"
        exit 1
    fi
    
    if ! systemctl is-active --quiet docker; then
        log_warn "Docker 服务未运行，尝试启动..."
        systemctl start docker || {
            log_err "无法启动 Docker 服务"
            exit 1
        }
    fi
    
    if netstat -tlnp 2>/dev/null | grep -q ":${PHP_PORT} " || ss -tlnp 2>/dev/null | grep -q ":${PHP_PORT} "; then
        log_warn "端口 ${PHP_PORT} 已被占用:"
        netstat -tlnp 2>/dev/null | grep ":${PHP_PORT} " || ss -tlnp | grep ":${PHP_PORT} "
        read -p "是否继续? [y/N]: " -r
        [[ ! $REPLY =~ ^[Yy]$ ]] && exit 1
    fi
    
    log_ok "环境检查通过"
}

#===============================================================================
# 配置 Docker 镜像加速器
#===============================================================================

setup_mirror() {
    log_info "配置 Docker 镜像加速器..."
    
    DAEMON_JSON="/etc/docker/daemon.json"
    
    if [[ -f "${DAEMON_JSON}" ]]; then
        cp "${DAEMON_JSON}" "${DAEMON_JSON}.bak.$(date +%Y%m%d%H%M%S)"
    fi
    
    local mirror_list=""
    for mirror in "${MIRRORS[@]}"; do
        mirror_list="${mirror_list}\"${mirror}\", "
    done
    mirror_list="${mirror_list%, }"
    
    cat > "${DAEMON_JSON}" << EOF
{
    "registry-mirrors": [${mirror_list}],
    "max-concurrent-downloads": 10,
    "log-driver": "json-file",
    "log-opts": {
        "max-size": "10m",
        "max-file": "3"
    }
}
EOF
    
    systemctl daemon-reload
    systemctl restart docker
    sleep 2
    
    log_ok "镜像加速器配置完成"
}

#===============================================================================
# 拉取镜像（带重试）
#===============================================================================

pull_image() {
    local image="php:${PHP_VERSION}-fpm"
    local max_retries=3
    local retry=0
    
    log_info "拉取镜像: ${image}"
    
    while [[ $retry -lt $max_retries ]]; do
        if docker pull "${image}" 2>&1; then
            log_ok "镜像拉取成功"
            return 0
        fi
        
        retry=$((retry+1))
        log_warn "拉取失败 (第 ${retry}/${max_retries} 次)"
        
        if [[ $retry -eq 1 ]]; then
            setup_mirror
        fi
        
        sleep 2
    done
    
    log_err "标准版拉取失败，尝试 Alpine 版本..."
    if docker pull "php:${PHP_VERSION}-fpm-alpine"; then
        log_warn "已切换到 Alpine 版本（体积更小）"
        return 0
    fi
    
    log_err "所有方式都失败，请检查网络"
    exit 1
}

#===============================================================================
# 清理旧安装
#==========================HP_VERSION}-fpm-alpine$"; then
        php_image="php:${PHP_VERSION}-fpm-alpine"
        log_info "使用 Alpine 镜像（更小更快）"
    fi
    
    docker run -d \
        --name "${CONTAINER_NAME}" \
        --restart always \
        --network host \
        -v "${WEB_ROOT}:/var/www/html:ro" \
        -v "${APP_ROOT}:${APP_ROOT}:ro" \
        -v "${INSTALL_DIR}/php.ini:/usr/local/etc/php/php.ini:ro" \
        -v "${INSTALL_DIR}/www.conf:/usr/local/etc/php-fpm.d/www.conf:ro" \
        "${php_image}"
    
    sleep 3
    
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log_err "容器启动失败:"
        docker logs "${CONTAINER_NAME}"
        exit 1
    fi
    
    log_ok "PHP-FPM 启动成功"
}

#===============================================================================
# 验证安装
#===============================================================================

verify() {
    echo ""
    echo "==============================================================================================================

cleanup_old() {
    log_info "清理旧版本..."
    
    if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        docker stop "${CONTAINER_NAME}" 2>/dev/null || true
        docker rm "${CONTAINER_NAME}" 2>/dev/null || true
        log_info "已删除旧容器"
    fi
    
    mkdir -p "${WEB_ROOT}"
}

#===============================================================================
# 生成配置文件
#===============================================================================

create_configs() {
    log_info "创建配置文件..."
    
    # PHP-FPM 配置
    cat > "${INSTALL_DIR}/www.conf" << 'EOF'
[www]
user = www-data
group = www-data
listen = 9000
listen.owner = www-data
listen.group = www-data
listen.mode = 0660
pm = dynamic
pm.max_children = 50
pm.start_servers = 5
pm.min_spare_servers = 5
pm.max_spare_servers = 35
pm.max_requests = 500
access.log = /proc/self/fd/2
clear_env = no
catch_workers_output = yes
decorate_workers_output = no
EOF

    # PHP 配置
    cat > "${INSTALL_DIR}/php.ini" << 'EOF'
[PHP]
memory_limit = 256M
upload_max_filesize = 64M
post_max_size = 64M
max_execution_time = 300
date.timezone = Asia/Shanghai
display_errors = Off
log_errors = On
error_log = /proc/self/fd/2
EOF

    # 测试文件
    cat > "${WEB_ROOT}/index.php" << 'EOF'
<?php phpinfo(); ?>
EOF

    cat > "${WEB_ROOT}/ping.php" << 'EOF'
<?php
header('Content-Type: application/json');
echo json_encode(['status' => 'ok', 'php' => phpversion(), 'time' => date('Y-m-d H:i:s')]);
EOF
    
    log_ok "配置文件创建完成"
}

#===============================================================================
# 启动容器
#===============================================================================

setup_php() {
    log_info "启动容器: ${CONTAINER_NAME}"
    
    # 检查 Alpine 镜像是否存在，优先使用
    local php_image="php:${PHP_VERSION}-fpm"
    if docker images --format '{{.Repository}}:{{.Tag}}' | grep -q "^php:${P=========="
    log_ok "PHP ${PHP_VERSION} FPM 安装完成"
    echo "==================================================================="
    
    echo ""
    echo "【容器状态】"
    docker ps --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
    
    echo ""
    echo "【PHP 版本】"
    docker exec "${CONTAINER_NAME}" php -v | head -2
    
    echo ""
    echo "【端口监听】"
    ss -tlnp 2>/dev/null | grep :9000 || echo "  127.0.0.1:9000"
    
    echo ""
    echo "【目录结构】"
    echo "  ${INSTALL_DIR}/"
    echo "  ├── www/index.php    # phpinfo"
    echo "  ├── www/ping.php     # 健康检查"
    echo "  ├── php.ini          # PHP 配置"
    echo "  └── www.conf         # FPM 配置"
    
    echo ""
    echo "【Nginx 配置示例】"
    echo ""
    echo "  location ~ \.php\$ {"
    echo "      fastcgi_pass   127.0.0.1:9000;"
    echo "      fastcgi_index  index.php;"
    echo "      fastcgi_param  SCRIPT_FILENAME  ${WEB_ROOT}\$fastcgi_script_name;"
    echo "      include        fastcgi_params;"
    echo "  }"
    echo ""
    echo "  root   ${WEB_ROOT};"
    echo "  index  index.php index.html;"
    
    echo ""
    echo "【常用命令】"
    echo "  查看日志:  docker logs ${CONTAINER_NAME}"
    echo "  进入容器:  docker exec -it ${CONTAINER_NAME} sh"
    echo "  重启:      docker restart ${CONTAINER_NAME}"
    echo "  停止:      docker stop ${CONTAINER_NAME}"
    
    echo ""
    echo "==================================================================="
    echo "  测试: curl http://localhost/ping.php"
    echo "==================================================================="
}

#===============================================================================
# 主流程
#===============================================================================

main() {
    echo "==================================================================="
    echo "  Docker 安装 PHP ${PHP_VERSION} FPM"
    echo "  目标: Nginx fastcgi_pass 127.0.0.1:${PHP_PORT}"
    echo "==================================================================="
    echo ""
    
    check_requirements
    cleanup_old
    create_configs
    pull_image
    setup_php
    verify
}

main "$@"
