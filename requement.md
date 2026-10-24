# 权重获取备忘

```bash
# 在仓库根目录执行（clone 下来的任意目录都行）
pip install modelscope
modelscope download --model cyankiwi/Qwen3.8-27B-AWQ-INT4 \
    --local_dir ./weight/Qwen3.8-27B-AWQ-INT4
```

更省事的方式：`make weight`（已完整会跳过，可反复执行；中断后重跑会续传）。
