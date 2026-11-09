"""FFmpeg 领域包：命令组装、progress 协议与子进程托管。

- command：output_filename 渲染、参数模板填充、程序预置参数与 part 文件命名；
- progress：progress 协议块的解析与中文进度行渲染；
- execute：ffmpeg 子进程托管（停止 / 超时 / 中止终审）与进度消费循环。
"""
