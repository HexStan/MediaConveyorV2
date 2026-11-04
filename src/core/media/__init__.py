"""MediaConveyor 的领域层：媒体属性探测、过滤与 FFmpeg 执行。

- probe：ffprobe 封装，产出统一的媒体属性字典；
- parsing：过滤条件配置值的解析器（容量 / 时长 / 码率）；
- filter：文件名分类流水线与媒体属性匹配；
- ffmpeg：命令组装（模板填充与程序预置参数注入）、子进程托管与 progress 协议进度消费。
"""
