# 自建镜像：使用已审计并固定的源码，构建时打补丁，不依赖作者推送的 latest 镜像。
# 建议把 FROM 的基础镜像换成具体 digest（node:22-alpine@sha256:...）以完全锁定供应链。
FROM node:22-alpine

WORKDIR /app
COPY src/weread-challenge.js package.json package-lock.json harden.mjs ./

RUN node harden.mjs weread-challenge.js \
 && node --check weread-challenge.js \
 && grep -q 'harden: never allow remote telemetry' weread-challenge.js \
 && npm ci --omit=dev \
 && rm -f harden.mjs

# 8788 端口在本部署中通过 WEREAD_API_PORT=0 禁用，不对外监听
CMD ["node", "weread-challenge.js", "run"]
