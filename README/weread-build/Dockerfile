# Patched local build. Base is a PINNED local tag of the official image:
# no Docker Hub pull, no 'latest' drift. node_modules and the Node runtime
# are inherited, so we only replace the single patched source file.
FROM weread-base:pinned-20261113

WORKDIR /app
COPY ./src/weread-challenge.js ./app.js

CMD ["node", "app.js"]