"""gunicorn 設定(add-observability-stack design.md D2)。

參數沿用原本 Dockerfile `CMD`。另外處理 prometheus_client multiprocess mode:

- `on_starting`(master 啟動、fork worker 前):清空並重建 `PROMETHEUS_MULTIPROC_DIR`,
  app 整體重啟後不殘留上次的數值。目錄在 container 自己的 /dev/shm(tmpfs)。
- `child_exit`(worker 結束):`mark_process_dead(worker.pid)` 移除該 worker 的 live
  gauge 檔;counter / histogram 檔保留,已累計的數值仍在加總中。

未設 `PROMETHEUS_MULTIPROC_DIR`(例如本機直接跑 gunicorn、celery worker 不會用到本檔)時
兩個 hook 都不做事;設了但為空白時 `on_starting` 拋錯,gunicorn 不啟動。
Guarded by config/tests/test_gunicorn_conf.py.
"""

import os
import shutil

from prometheus_client.multiprocess import mark_process_dead

bind = "0.0.0.0:8000"
worker_class = "gthread"
workers = 3
threads = 4
timeout = 60
worker_tmp_dir = "/dev/shm"


def _multiproc_dir():
    """未設定回 None;設了但為空白則拋錯——prometheus_client 只看 key 是否存在,
    空字串仍會進 multiprocess mode 並把 .db 檔寫進 CWD,且不會被清理 / mark dead。"""
    path = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if path is None:
        return None
    if not path.strip():
        raise RuntimeError("PROMETHEUS_MULTIPROC_DIR is set but blank; set a directory or unset it")
    return path


def on_starting(server):
    path = _multiproc_dir()
    if path is None:
        return
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)


def child_exit(server, worker):
    path = _multiproc_dir()
    if path is None:
        return
    mark_process_dead(worker.pid, path)
