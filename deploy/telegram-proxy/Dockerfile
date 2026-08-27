FROM alpine:3.22

RUN apk add --no-cache openssh-client \
    && adduser -D -H -u 10002 proxy

USER 10002:10002
ENTRYPOINT ["/usr/bin/ssh"]
