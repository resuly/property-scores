# Gunicorn config for property-scores API.
#
# 为什么从 `uvicorn --workers 2` 换成 gunicorn+UvicornWorker（2026-08-25）：
# uvicorn 0.30+ 的 multiprocess supervisor 每 0.5s 对 worker 做 pipe ping，
# 默认 5s 收不到 pong 就直接 SIGKILL（"Child process died"，无任何现场）。
# noise 冷请求单次 ~7.6s 重计算，加上 cgroup 骑到 memory.high 后分配路径被
# 内核 direct-reclaim 限速（拿着 GIL 卡顿），pong 线程被饿死 >5s，于是
# 生产上 worker 每 ~7.4h 被误杀一次，正好落在 noise 请求高峰。
# gunicorn 的 arbiter 用 worker 主动 notify + 宽 timeout，超时先报
# `WORKER TIMEOUT (pid:N)` 再 SIGABRT（有定性与 pid；post_worker_init 在
# Gunicorn 重置信号后启用 faulthandler，补全线程现场），且 max_requests
# 让内存回收发生在计划内的请求间隙（graceful，先答完在手请求）。
#
# 生产 ExecStart（unit 文件不入库，改动记录见 CHANGES.md）：
#   .venv/bin/gunicorn property_scores.api.main:app -c gunicorn_conf.py

import faulthandler

bind = "127.0.0.1:8099"
workers = 2
worker_class = "uvicorn_worker.UvicornWorker"

# gunicorn 默认 accesslog=None，请求行会整体消失（uvicorn-worker 把空 handler
# 复制给 uvicorn.access 且 propagate=False）。而 journal 里的请求行正是
# 2026-08-25 定位 worker 被误杀的证据链，必须保留。"-" = stdout → journal
# （gunicorn 26.2 对 access 流硬编码 stdout；生命周期日志走 stderr，都进 journal）。
accesslog = "-"

# 实测生产 ~1,444 req/24h（2026-08-24 journal），约 30 req/h/worker：
# jitter 只加不减，实际 500-600 请求 ≈ 16.7-20h 回收一个 worker，
# 两个 worker 被 jitter 错开。回收把 RSS 增长（实测 +130MB/天）清零，
# cgroup 不再爬到 memory.high(3200M) 的限速线。
max_requests = 500
max_requests_jitter = 100

# noise 冷路径实测 7.6s（RF 首载）、transfer 密集区 1.7-2.2s/点；
# 下游 DA Leads 客户端超时 28s。60s 只拦真死锁，不拦慢请求。
timeout = 60
graceful_timeout = 30

# 与 uvicorn 时代一致：不 preload。rf.pkl(114MB) 本就是首个 noise 请求
# 才懒加载，preload 共享不到它，反而让 fork 与 GDAL/rasterio 线程状态纠缠。
preload_app = False

# Gunicorn 25.1+ otherwise creates its control socket by default. This service
# is managed exclusively through systemd signals and does not use gunicornc;
# keep the unused local control plane closed.
control_socket_disable = True


def post_worker_init(worker):
    """Install fatal-signal traceback capture after Gunicorn resets signals.

    Gunicorn 26.2 Worker.init_process() runs init_signals(), loads the app, then
    invokes this hook. Enabling earlier is ineffective because init_signals
    replaces SIGABRT; enabling here preserves a Python all-thread dump when the
    arbiter aborts a timed-out worker.
    """
    faulthandler.enable(all_threads=True)
