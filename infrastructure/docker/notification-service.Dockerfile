# syntax=docker/dockerfile:1.7
# notification-service (Go, Redis Streams consumer) — build context: repository root.
#   docker build -f infrastructure/docker/notification-service.Dockerfile .                # runtime image
#   docker build -f infrastructure/docker/notification-service.Dockerfile --target test .  # vet + tests

ARG GO_VERSION=1.27

FROM golang:${GO_VERSION}-alpine AS builder
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
# Download modules first so this layer is cached until go.mod/go.sum change.
COPY shared/go.mod shared/go.sum ./shared/
COPY services/notification-service/go.mod services/notification-service/go.sum ./services/notification-service/
WORKDIR /src/services/notification-service
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY shared /src/shared
COPY services/notification-service /src/services/notification-service

FROM builder AS test
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    test -z "$(gofmt -l . ../../shared)" && go vet ./... && go test ./...

FROM builder AS build
ARG VERSION=dev
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/notification-service ./cmd/notification-service

FROM gcr.io/distroless/static-debian12:nonroot AS runtime
COPY --from=build /out/notification-service /notification-service
USER nonroot:nonroot
EXPOSE 8090
# Distroless has no shell/curl: the binary probes itself.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD ["/notification-service", "healthcheck"]
ENTRYPOINT ["/notification-service"]
