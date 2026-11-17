# syntax=docker/dockerfile:1.7
# api-gateway (Go, net/http reverse proxy) — build context: repository root.
#   docker build -f infrastructure/docker/api-gateway.Dockerfile .                # runtime image
#   docker build -f infrastructure/docker/api-gateway.Dockerfile --target test .  # vet + tests

ARG GO_VERSION=1.27

FROM golang:${GO_VERSION}-alpine AS builder
ENV CGO_ENABLED=0 GOTOOLCHAIN=local
WORKDIR /src
# Download modules first so this layer is cached until go.mod/go.sum change.
COPY shared/go.mod shared/go.sum ./shared/
COPY services/api-gateway/go.mod services/api-gateway/go.sum ./services/api-gateway/
WORKDIR /src/services/api-gateway
RUN --mount=type=cache,target=/go/pkg/mod go mod download
COPY shared /src/shared
COPY services/api-gateway /src/services/api-gateway

FROM builder AS test
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    test -z "$(gofmt -l . ../../shared)" && go vet ./... && go test ./...

FROM builder AS build
ARG VERSION=dev
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    go build -trimpath -ldflags="-s -w -X main.version=${VERSION}" -o /out/api-gateway ./cmd/api-gateway

FROM gcr.io/distroless/static-debian12:nonroot AS runtime
COPY --from=build /out/api-gateway /api-gateway
USER nonroot:nonroot
EXPOSE 8080
# Distroless has no shell/curl: the binary probes itself.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD ["/api-gateway", "healthcheck"]
ENTRYPOINT ["/api-gateway"]
