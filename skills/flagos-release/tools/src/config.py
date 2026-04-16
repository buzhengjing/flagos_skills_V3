"""
配置管理模块
从 context.yaml 加载配置，并提供配置验证和自动填充
"""
import os
import subprocess
from dataclasses import dataclass, field
from typing import List
import yaml

from .chip_detector import ChipDetector, ChipVendor, VENDOR_NAMES, sanitize_docker_tag


@dataclass
class ChipConfig:
    """芯片配置"""
    # 芯片厂商，默认自动检测
    vendor: str = "auto"
    # Harbor 仓库地址
    harbor_registry: str = "harbor.baai.ac.cn/flagrelease-public"
    # 以下为内部使用，自动检测填充
    auto_generate_tag: bool = True
    tree: str = "none"
    gems_version: str = ""
    scale_version: str = ""
    cx: str = "none"
    date_tag: str = ""
    driver_version: str = ""
    sdk_version: str = ""
    torch_version: str = ""
    python_version: str = ""
    gpu_model: str = ""


@dataclass
class PublishConfig:
    """发布阶段配置"""
    enabled: bool = True
    # 镜像发布
    tag_image: bool = True
    push_harbor: bool = True
    # README 生成
    generate_readme: bool = True
    readme_output_path: str = "./README.md"
    # 模型发布
    publish_modelscope: bool = True
    modelscope_model_id: str = ""
    modelscope_token: str = ""
    publish_huggingface: bool = True
    huggingface_repo_id: str = ""
    huggingface_token: str = ""
    # 权重文件上传
    upload_weights: bool = True
    weights_dir: str = ""
    # 自动读取评测结果目录（步骤4/5产出），填入 README
    results_dir: str = ""
    # 仓库可见性
    private: bool = True
    # 已有的 Harbor 镜像地址（跳过 commit/tag/push）
    existing_harbor_image: str = ""
    # 内部使用
    image_source: str = ""
    image_target_tag: str = ""
    harbor_path: str = ""
    readme_script_path: str = ""
    upload_files: List[str] = field(default_factory=list)


@dataclass
class ModelInfo:
    """模型信息配置"""
    # 必填：模型来源
    source_of_model_weights: str = ""  # 如 "Qwen/Qwen3-8B"
    # 可选：模型介绍
    new_model_introduction: str = ""
    # 可选：评测结果
    evaluation_results: List[dict] = field(default_factory=list)
    # 以下全部自动生成
    output_name: str = ""
    vendor: str = ""
    docker_version: str = ""
    ubuntu_version: str = ""
    flagrelease_name: str = ""
    flagrelease_name_pre: str = ""
    image_harbor_path: str = ""
    container_run_cmd: str = ""
    serve_start_cmd: str = ""
    serve_infer_cmd: str = ""


@dataclass
class PipelineConfig:
    """完整的流水线配置"""
    input_type: str = "container"
    container_name: str = ""
    host_workspace_base: str = ""  # /data/flagos-workspace/<model>，由 context.yaml workspace.host_path 填充

    # 各阶段配置
    chip: ChipConfig = field(default_factory=ChipConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    model_info: ModelInfo = field(default_factory=ModelInfo)

    # 执行哪些阶段
    stages_to_run: List[str] = field(default_factory=lambda: ["publish"])


def load_config_from_context(context_path: str) -> PipelineConfig:
    """从 FlagOS context.yaml 自动构建发布配置，无需手写 YAML 配置文件。

    context.yaml 是 FlagOS 工作流各步骤的共享状态，包含容器名、模型信息、
    评测结果、GPU 信息等。本函数将这些字段映射为 PipelineConfig。
    """
    with open(context_path, 'r', encoding='utf-8') as f:
        ctx = yaml.safe_load(f)

    config = PipelineConfig()
    config.input_type = 'container'
    config.container_name = ctx.get('container', {}).get('name', '')
    config.stages_to_run = ['publish']

    # ---- model_info ----
    model = ctx.get('model', {})
    config.model_info.source_of_model_weights = model.get('name', '')

    # evaluation_results
    ev = ctx.get('eval', {})
    if ev.get('v1_score') is not None and ev.get('v2_score') is not None:
        method = ev.get('eval_method', 'GPQA_Diamond')
        config.model_info.evaluation_results = [
            {'metric': method, 'origin': ev['v1_score'], 'flagos': ev['v2_score']}
        ]

    # serve_start_cmd
    svc = ctx.get('service', {})
    runtime = ctx.get('runtime', {})
    port = svc.get('port', 8000)
    tp = runtime.get('tp_size', 1)
    model_path = model.get('container_path', '')
    max_model_len = svc.get('max_model_len', '')
    cmd_parts = [f"vllm serve {model_path}",
                 f"--host 0.0.0.0 --port {port}",
                 f"--tensor-parallel-size {tp}"]
    if max_model_len:
        cmd_parts.append(f"--max-model-len {max_model_len}")
    config.model_info.serve_start_cmd = " \\\n".join(cmd_parts)

    # container_run_cmd (通用模板，IMAGE 占位符由 auto_fill_config 替换)
    config.model_info.container_run_cmd = (
        "docker run --init --detach --net=host --uts=host --ipc=host "
        "--security-opt=seccomp=unconfined --privileged=true "
        "--ulimit stack=67108864 --ulimit memlock=-1 "
        "--ulimit nofile=1048576:1048576 --shm-size=32G "
        "-v /data:/data --gpus all --name flagos {{IMAGE}} sleep infinity\n"
        "docker exec -it flagos /bin/bash"
    )

    # ---- chip ----
    gpu = ctx.get('gpu', {})
    config.chip.vendor = gpu.get('vendor', 'auto')

    # ---- publish ----
    config.publish.tag_image = True
    config.publish.push_harbor = True
    # 从 workflow.qualified 判定发布可见性：qualified=true → 公开，否则私有
    workflow = ctx.get('workflow', {})
    config.publish.private = not workflow.get('qualified', False)
    config.publish.upload_weights = True
    # 优先用 local_path（宿主机路径），其次 container_path（容器内路径）
    # 镜像模式下 local_path 是用户提供的宿主机路径，一定能访问
    # 容器模式下两者可能相同（容器内路径），宿主机未必能访问，publish.py 有 docker cp 兜底
    config.publish.weights_dir = model.get('local_path', '') or model.get('container_path', '')
    config.publish.publish_modelscope = False
    config.publish.publish_huggingface = False

    # 如果 context 中已有完整的 Harbor 镜像地址，跳过 commit/tag/push
    # 优先使用 release.image_tag（完整 URL），其次 image.tag（仅当它是完整 URL 时）
    release = ctx.get('release', {})
    existing_tag = release.get('image_tag', '')
    if not existing_tag or '/' not in existing_tag:
        image = ctx.get('image', {})
        candidate = image.get('tag', '')
        if candidate and '/' in candidate:
            existing_tag = candidate
        else:
            existing_tag = ''
    if existing_tag:
        config.publish.existing_harbor_image = existing_tag

    # token 从宿主机环境变量读取，若不存在则尝试从容器内获取
    config.publish.modelscope_token = os.environ.get('MODELSCOPE_TOKEN', '')
    config.publish.huggingface_token = os.environ.get('HF_TOKEN', '')

    if (not config.publish.modelscope_token or not config.publish.huggingface_token) and config.container_name:
        for env_var, attr in [('MODELSCOPE_TOKEN', 'modelscope_token'), ('HF_TOKEN', 'huggingface_token')]:
            if not getattr(config.publish, attr):
                try:
                    result = subprocess.run(
                        ["docker", "exec", config.container_name, "printenv", env_var],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        setattr(config.publish, attr, result.stdout.strip())
                except Exception:
                    pass

    # 有 token 则启用对应平台上传
    config.publish.publish_modelscope = bool(config.publish.modelscope_token)
    config.publish.publish_huggingface = bool(config.publish.huggingface_token)
    # results_dir 用于 README 自动读取评测结果
    workspace = ctx.get('workspace', {})
    container_workspace = workspace.get('container_path', '/flagos-workspace')
    config.publish.results_dir = f"{container_workspace}/results"

    # 宿主机工作目录（数据回传目标，应为 /data/flagos-workspace/<model> 格式）
    config.host_workspace_base = workspace.get('host_path') or ''

    return config


def validate_config(config: PipelineConfig) -> List[str]:
    """验证配置是否完整"""
    errors = []

    if not config.container_name:
        errors.append("container_name is required (from context.yaml container.name)")

    if 'publish' in config.stages_to_run and config.publish.enabled:
        if not config.model_info.source_of_model_weights:
            errors.append("model_info.source_of_model_weights is required (e.g., 'Qwen/Qwen3-8B')")

        if config.publish.publish_modelscope and not config.publish.modelscope_token:
            errors.append("publish.modelscope_token is required (or set MODELSCOPE_TOKEN env)")

        if config.publish.publish_huggingface and not config.publish.huggingface_token:
            errors.append("publish.huggingface_token is required (or set HF_TOKEN env)")

    return errors


def _extract_model_name(source: str) -> str:
    """从模型来源提取模型名称"""
    if not source:
        return ""
    if "/" in source:
        return source.split("/")[-1]
    return source


def _clean_model_name_for_tag(name: str) -> str:
    """清理模型名称用于生成 tag"""
    import re
    clean = re.sub(r'[^a-zA-Z0-9-]', '-', name.lower())
    clean = re.sub(r'-+', '-', clean).strip('-')
    return clean


def auto_fill_config(config: PipelineConfig) -> PipelineConfig:
    """根据环境检测自动填充配置中的空字段"""
    import datetime
    import re

    # 确定容器名称
    container = config.container_name

    # 创建检测器
    detector = ChipDetector(container_name=container if container else None)

    # 解析 vendor
    vendor = None
    if config.chip.vendor and config.chip.vendor != "auto":
        try:
            vendor = ChipVendor(config.chip.vendor)
        except ValueError:
            pass

    # 检测环境信息
    try:
        env_info = detector.detect_environment(vendor=vendor)
    except Exception:
        env_info = None

    # ==================== 芯片和系统信息 ====================
    if env_info:
        if config.chip.vendor == "auto" and env_info.vendor:
            config.chip.vendor = env_info.vendor.value

        if not config.model_info.vendor and env_info.vendor_cn_name:
            config.model_info.vendor = env_info.vendor_cn_name

        if not config.model_info.docker_version and env_info.docker_version:
            config.model_info.docker_version = env_info.docker_version

        if not config.model_info.ubuntu_version:
            if env_info.os_name and env_info.os_version:
                config.model_info.ubuntu_version = f"{env_info.os_name} {env_info.os_version}"
            elif env_info.os_version:
                config.model_info.ubuntu_version = env_info.os_version

        if not config.chip.driver_version and env_info.driver_version:
            config.chip.driver_version = env_info.driver_version
        if not config.chip.sdk_version and env_info.sdk_version:
            config.chip.sdk_version = env_info.sdk_version
        if not config.chip.torch_version and env_info.torch_version:
            config.chip.torch_version = env_info.torch_version
        if not config.chip.python_version and env_info.python_version:
            config.chip.python_version = env_info.python_version
        if not config.chip.gpu_model and env_info.gpu_model:
            config.chip.gpu_model = env_info.gpu_model

        if env_info.flaggems_version:
            config.chip.gems_version = env_info.flaggems_version
        if env_info.flagscale_version:
            config.chip.scale_version = env_info.flagscale_version

    # ==================== 模型名称 ====================
    model_name = _extract_model_name(config.model_info.source_of_model_weights)
    vendor_name = config.chip.vendor or "unknown"

    if not config.model_info.output_name and model_name:
        if vendor_name == "nvidia":
            config.model_info.output_name = model_name
        else:
            config.model_info.output_name = f"{model_name}-{vendor_name}"

    if not config.model_info.flagrelease_name and config.model_info.output_name:
        config.model_info.flagrelease_name = f"{config.model_info.output_name}-FlagOS"

    if not config.model_info.flagrelease_name_pre and model_name:
        match = re.match(r'^([A-Za-z]+\d*)', model_name)
        if match:
            config.model_info.flagrelease_name_pre = match.group(1)
        else:
            config.model_info.flagrelease_name_pre = model_name.split('-')[0]

    # ==================== 镜像 tag ====================
    if not config.chip.date_tag:
        config.chip.date_tag = datetime.datetime.now().strftime("%Y%m%d%H%M")

    if not config.publish.image_target_tag and config.chip.auto_generate_tag:
        from .chip_detector import ChipVersionInfo, generate_image_tag as _generate_tag
        chip_info = ChipVersionInfo(
            vendor=ChipVendor(vendor_name) if vendor_name and vendor_name != "unknown" else None,
            driver_version=config.chip.driver_version,
            sdk_version=config.chip.sdk_version,
            torch_backend=env_info.torch_backend if env_info and env_info.torch_backend else "",
            torch_version=config.chip.torch_version,
            python_version=config.chip.python_version,
            gpu_model=config.chip.gpu_model,
            arch=env_info.arch if env_info and env_info.arch else "amd64",
        ) if vendor_name and vendor_name != "unknown" else None

        if chip_info:
            config.publish.image_target_tag = _generate_tag(
                info=chip_info,
                model_name=model_name or "unknown",
                harbor_registry=config.chip.harbor_registry,
                tree=config.chip.tree,
                gems_version=config.chip.gems_version,
                scale_version=config.chip.scale_version,
                cx=config.chip.cx,
                date_tag=config.chip.date_tag,
            )

    if not config.publish.harbor_path and config.publish.image_target_tag:
        config.publish.harbor_path = config.publish.image_target_tag

    if not config.model_info.image_harbor_path and config.publish.image_target_tag:
        config.model_info.image_harbor_path = config.publish.image_target_tag

    # ==================== ModelScope / HuggingFace ID ====================
    if not config.publish.modelscope_model_id and config.model_info.flagrelease_name:
        config.publish.modelscope_model_id = f"FlagRelease/{config.model_info.flagrelease_name}"

    if not config.publish.huggingface_repo_id and config.model_info.flagrelease_name:
        config.publish.huggingface_repo_id = f"FlagRelease/{config.model_info.flagrelease_name}"

    # ==================== 命令 ====================
    if config.model_info.container_run_cmd and config.publish.image_target_tag:
        config.model_info.container_run_cmd = config.model_info.container_run_cmd.replace(
            '{{IMAGE}}', config.publish.image_target_tag
        )

    if not config.model_info.serve_infer_cmd:
        api_endpoint = "http://localhost:8000/v1"
        api_endpoint_masked = re.sub(r'http://[\d.]+:', 'http://<ip>:', api_endpoint)
        model_id = config.model_info.output_name.lower().replace('-', '_') if config.model_info.output_name else "model"
        config.model_info.serve_infer_cmd = f'''from openai import OpenAI

client = OpenAI(
    api_key="EMPTY",
    base_url="{api_endpoint_masked}"
)

response = client.chat.completions.create(
    model="{model_id}",
    messages=[
        {{"role": "system", "content": "You are a helpful assistant."}},
        {{"role": "user", "content": "Hello!"}}
    ]
)
print(response.choices[0].message.content)'''

    # ==================== 上传文件列表 ====================
    if not config.publish.upload_files:
        config.publish.upload_files = [config.publish.readme_output_path]

    if config.publish.upload_weights and not config.publish.weights_dir:
        pass  # weights_dir 必须在配置中显式指定

    return config
