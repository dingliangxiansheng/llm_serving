# 常用操作入口：make help 查看
.DEFAULT_GOAL := help
SHELL := /usr/bin/env bash
PY := .venv-gateway/bin/python

.PHONY: help setup install test lint check-paths weight weight-check branding up down logs ps smoke run stop venv

help: ## 显示所有目标
	@grep -hE '^[a-zA-Z_]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## 一键准备（体检 + 建 venv + 下权重；容器内可跑，不需要 docker）
	./scripts/bootstrap.sh

install: ## 建 venv 并安装全部依赖（uv）
	./scripts/install.sh

test: ## 跑网关单测（不需要 GPU）
	$(PY) -m pytest tests -q

lint: check-paths ## ruff 静态检查 + 格式检查 + 可移植性检查
	$(PY) -m ruff check gateway tests
	$(PY) -m ruff format --check gateway tests

check-paths: ## 检查仓库里是否混进本机绝对路径（应全部用相对路径）
	./scripts/check_paths.sh

weight: ## 用 modelscope 下载权重到 ./weight/<模型名>（已完整则跳过）
	./scripts/fetch_weight.sh

weight-check: ## 只校验 ./weight 下权重是否完整（不下载）
	./scripts/fetch_weight.sh --check

branding: ## 把站点署名样式装进本机 Open WebUI（Docker 部署已自动挂载，无需此步）
	./scripts/patch_webui_branding.sh

up: ## docker 一键部署（下载权重 + 构建 + 启动）
	./scripts/docker_up.sh

down: ## 停止容器（保留数据卷）
	docker compose down

logs: ## 跟踪全部容器日志
	docker compose logs -f

ps: ## 查看容器状态
	docker compose ps

smoke: ## 端到端自检（会真实调用模型）
	./scripts/smoke_test.sh

run: ## 不打 Docker，直接在本机拉起三个进程（vLLM + 网关 + WebUI）
	./scripts/start_all.sh

stop: ## 停止 start_all.sh 拉起的进程
	./scripts/stop_all.sh --gpu
