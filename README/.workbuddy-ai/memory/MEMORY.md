# 项目长期笔记

## 用户偏好
- 对隐私与数据外发敏感：安装/部署第三方开源项目前，先审计其对外网络请求（尤其是 cookies、遥测、登录凭证），并优先给出「禁止上传到第三方服务器」的方案再动手。
- 涉及 PVE / LXC / Docker 部署时，倾向容器化部署；对 `latest` 镜像、挂载 `/var/run/docker.sock` 等供应链与权限风险需要主动提示。

## 环境
- PVE 宿主：192.168.91.254（root / `qm2900880`，CST `Asia/Shanghai`）；CT 107：192.168.91.130（root / `2900880`，系统时钟 **UTC**，换算北京 +8）；工作机并不总在 91 网段，连不上时先查路由/Tailscale，不要假定故障在服务端。
- PVE cron 按 CST 解释；CT 107 内 crontab 按 UTC 写，且 CT 107 未装 `anacron`（关机会丢掉错过的任务）。
- 四脚本：A=PVE `/usr/local/sbin/pve-power-schedule.sh`（00:00 关 / 09:00 开）；B=PVE `/usr/local/sbin/pve-ct107-random-start.py`（08:00–08:30 随机开 107）；C=CT107 `/etc/systemd/system/weread-boot-start.service`（开机一次性）；D=CT107 `/usr/local/bin/weread-duration.py`（roll/wait/launch，随机时长+毫秒偏移+同日防护）。**CT 107 只由脚本 B 开机**，脚本 A 明确排除 107 的 start。
- 用户明确：「中心 73 / 08:10」指**众数（最常出现值）**，不是均值；实现为 `MU = MODE_MIN` + 截断正态，不要再做均值校准。

## 部署约定（踩过的坑）
- **用户强约束**：`WEREAD_AGREE_TERMS=false` 必须写入；除此以外的加固不要擅自加。改 compose 一律**在现有文件上增量改**，绝不能用仓库自带 `docker-compose.yml` 覆盖（会丢隐私行与整组邮件配置）；改前先备份到 `/root/…backup-<ts>/`。
- `C:/Users/Albot/Downloads/weread-challenge.js` 是 host-managed 附件快照，**会被环境还原**；补丁必须固化到工作区 `weread-build/src/weread-challenge.js`，构建/验证都从稳定路径读。
- 自建镜像用「固定本地官方镜像 tag → 只 COPY 覆盖 `/app/app.js`」，避免从 Docker Hub 拉 `node:lts-alpine` 卡住；compose 必须同步 `image: weread-challenge:local` + `pull_policy: never`。
- 改 env 后**必须 recreate 容器**才生效（不需要 build）；「compose 已改但运行中容器仍是旧镜像/env」是正常状态，等下次 `compose up -d --force-recreate app`。
- 跨 Docker stack 不能用服务名互访（会被解析成 fake-ip `198.18.8.123`）；用 LAN IP `192.168.91.130` 或网桥网关，或固化 `docker network connect` 别名。
- 验证全链路优先用 `docker compose run --rm --entrypoint …`（复用 live compose 的 image/env/network），不触发阅读、不重建 app、不改 `plan.json`。
- 部署脚本统一：SFTP 传**纯 bytes**（曾因 `zlib.compress` 配 `gzip -d` 写出 0 字节）、先写 `/tmp` + 非空 + SHA256 校验再 `mv`、备份、`py_compile` / `config -q` / `systemd-analyze verify` 后只读验收。
