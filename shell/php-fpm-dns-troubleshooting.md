# 一个接口从 15 秒降到几十毫秒：Docker PHP-FPM 排障复盘

服务器1运行 CentOS 7，Nginx 通过 Docker 中的 PHP-FPM 8.2.35 处理 PHP 请求；服务器2则在宿主机直接运行 PHP-FPM 7.4.33。两台机器部署了相同的程序、数据库和 Redis 环境，但服务器1的接口经常耗时 15 秒以上，服务器2访问正常。

排查结果表明，不能只盯着 PHP 版本或 SQL。慢请求先后卡在 Redis 连接和数据库主机名解析上。逐层测量后，接口最终恢复到几十毫秒。

## 先测清楚：时间花在 Nginx 还是 PHP-FPM

先为 Nginx 增加总耗时和 FastCGI 上游耗时日志：

```nginx
log_format timed '$remote_addr "$request" $status '
                 'rt=$request_time '
                 'uct=$upstream_connect_time '
                 'uht=$upstream_header_time '
                 'urt=$upstream_response_time '
                 'ua=$upstream_addr';

access_log /data0/www/kfcm_server/logs/host.timing.access.log timed;
```

一条慢请求的关键数据如下：

```text
request_time=15.026
upstream_connect_time=0.000
upstream_header_time=15.026
upstream_response_time=15.026
upstream_addr=127.0.0.1:9000
```

这说明 Nginx 到 PHP-FPM 的连接几乎立即建立，但 PHP-FPM 约 15 秒后才返回响应头。Nginx 本身和 FastCGI 连接不是主要耗时点。

还要确认请求实际命中了哪个 Nginx location。本文中的 `/index.php/...` 请求走 PHP-FPM；如果 URI 匹配专门的代理 location，则应排查代理上游，而不是 PHP-FPM。

## 开启 FPM 慢日志，直接看调用栈

在 FPM pool 配置中加入：

```ini
request_slowlog_timeout = 3s
request_slowlog_trace_depth = 30
slowlog = /tmp/php-fpm-slow.log
```

初次检查时，慢日志没有内容，FPM 错误日志却显示：

```text
failed to ptrace(ATTACH) child ...: Operation not permitted
```

FPM 正在尝试读取慢请求的调用栈，但容器权限不足。重建容器时，在原有 `docker run` 参数中加入：

```bash
--cap-add=SYS_PTRACE
```

之后慢日志才开始提供有效堆栈。增加该 capability 会扩大容器的进程跟踪权限，应按安全要求评估；不要为了方便直接使用 `--privileged`。

## 第一处瓶颈：Redis 会话连接

首次抓到的慢日志停在 Predis 的 `stream_socket_client()`，调用链位于应用执行 `session_start()` 时。也就是说，请求初始化 session 时正在建立 Redis 连接。

调整应用使用的 Redis 地址后，接口从约 15 秒降到了约 5 秒。这是一个重要线索：Redis 连接是主要延迟点之一。

## 第二处瓶颈：MySQL 主机名解析

之后的慢日志停在 Illuminate 的 `PDO::__construct()`，调用来自 `getReadPdo()`。此时 PHP 正在建立 MySQL 读连接，SQL 还没有开始执行。

当时容器中缺少 `pdo_mysql`，还出现过 `could not find driver`。这是独立的 PHP 扩展问题，可以通过自定义镜像解决。先在 PHP-FPM 项目目录创建或更新 `Dockerfile`：

```dockerfile
FROM php:8.2.35-fpm
RUN docker-php-ext-install pdo_mysql
```

如果现有镜像还安装了其他扩展、复制了配置文件或做过系统级定制，应在原有 Dockerfile 基础上补充安装步骤，不要直接用这个最小示例覆盖原镜像配置。

构建新镜像，并在启动容器前验证 PDO 驱动：

```bash
docker build -t php82-fpm:8.2.35-pdo-mysql .
docker run --rm --entrypoint php php82-fpm:8.2.35-pdo-mysql \
  -r 'var_export(PDO::getAvailableDrivers()); echo PHP_EOL;'
```

确认结果中包含 `mysql` 后，再切换容器。切换前先记录旧容器使用的镜像、端口映射、挂载目录、网络、环境变量和重启策略；敏感环境变量不要粘贴到工单或公开日志中。也可以从 Docker Compose 文件或部署脚本中确认原始启动参数。

本次使用 `docker run` 部署，先关闭旧容器的自动重启，停止并改名保留旧容器，再用新镜像创建同名容器。新容器沿用 host 网络、原有挂载和 FPM 慢日志所需的 `SYS_PTRACE` capability：

```bash
docker update --restart=no php82-fpm
docker stop php82-fpm
docker rename php82-fpm php82-fpm-old

docker run -d \
  --name php82-fpm \
  --restart always \
  --network host \
  --cap-add=SYS_PTRACE \
  -v /root/php82-docker/www:/var/www/html:ro \
  -v /data0/www/kfcm_server:/data0/www/kfcm_server:ro \
  -v /data0/www/kfcm_server/logs:/data0/www/kfcm_server/logs:rw \
  -v /root/php82-docker/php.ini:/usr/local/etc/php/php.ini:ro \
  -v /root/php82-docker/www.conf:/usr/local/etc/php-fpm.d/www.conf:ro \
  php82-fpm:8.2.35-pdo-mysql

# 检查扩展、容器状态及应用接口
docker exec php82-fpm php -r 'var_export(PDO::getAvailableDrivers()); echo PHP_EOL;'
docker logs --tail=100 php82-fpm
```

确认接口稳定后再决定何时清理旧容器。若新容器启动或验证失败，可以停止并改名新容器，恢复旧容器及其原来的自动重启策略：

```bash
docker stop php82-fpm
docker rename php82-fpm php82-fpm-new-failed
docker rename php82-fpm-old php82-fpm
docker update --restart=always php82-fpm
docker start php82-fpm
```

使用 Compose 管理容器时，应在 Compose 配置中更新镜像并按项目现有流程重建；同样要保留旧镜像或旧容器，验证通过后再清理。

但安装驱动本身并没有解释剩余的 5 秒。继续测数据库主机名解析，发现容器中的 `gethostbyname()` 耗时约 5 秒，而对应 Nginx 请求总耗时也约 5 秒。

## 找到 DNS 转发异常

宿主机和容器的 `/etc/resolv.conf` 都把 `127.0.0.1` 列为第一 DNS。本机运行着 dnsmasq，也监听在 `127.0.0.1:53`，但直接向它查询内网数据库域名和公网域名都会超时。绕过本机 dnsmasq，直接查询后面的内部 DNS，则能立即得到结果。

检查发现 dnsmasq 配置只有监听相关内容，没有明确的上游 DNS。由于系统解析配置首项又指回 `127.0.0.1`，本地 DNS 转发链路很可能出现了自指或上游配置问题。

为 dnsmasq 显式指定已验证可用的内部 DNS，并避免它继续从指回自身的 `resolv.conf` 读取上游：

```ini
listen-address=127.0.0.1
interface=lo
bind-interfaces

no-resolv
server=<内部DNS地址1>
server=<内部DNS地址2>
```

修改前先备份，随后验证配置并在合适时段重启 dnsmasq：

```bash
dnsmasq --test
systemctl status dnsmasq
systemctl restart dnsmasq
```

修复后，本机 dnsmasq 对内网数据库域名的查询约为 2 毫秒，容器中的 `gethostbyname()` 降到约 1 毫秒；公网域名查询也恢复正常。DNS 配置文件由 NetworkManager 管理时，不要只手动编辑生成的 `/etc/resolv.conf`，否则修改可能被覆盖。

## 结果与经验

修复后，接口响应恢复到几十毫秒以内。结合各阶段证据，本次排障识别出两个重要等待点：调整 Redis 地址后，耗时从约 15 秒降至约 5 秒；修复 dnsmasq 上游后，数据库主机名解析从约 5 秒降到毫秒级。

这次排障有几点值得复用：

1. **先量化，再优化。** `request_time`、`upstream_connect_time` 和 `upstream_response_time` 能帮助区分 Nginx、FastCGI 与应用内部的耗时。
2. **慢日志比猜测更直接。** 堆栈停在 Predis、PDO 构造函数还是 SQL 执行阶段，代表完全不同的排查方向。
3. **TCP 连通不等于域名解析正常。** 指定 IP 的 TCP 测试成功，并不能说明应用使用的主机名能及时解析。
4. **PDO 构造慢，不代表 SQL 慢。** 如果堆栈还在 `PDO::__construct()`，先查读库地址、DNS、握手和认证。
5. **容器网络正常，也可能继承宿主机的 DNS 问题。** `--network host` 没有消除本机 resolver 的延迟。
6. **扩展错误与请求延迟要分开处理。** `pdo_mysql` 缺失会导致驱动错误，但不是这次 5 秒 DNS 延迟的根因。
7. **临时操作要考虑持久性和安全边界。** 扩展应构建进镜像；启用 `SYS_PTRACE` 要评估权限范围；NetworkManager 管理的 DNS 应通过对应配置持久修改。

排查性能问题时，最重要的往往不是先升级或降级 PHP，而是找到请求时间线里究竟哪一步在等待。
