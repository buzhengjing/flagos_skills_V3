"""
配置管理模块
支持从 YAML 文件加载配置，并提供配置验证
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional
import yaml

from .chip_detector import ChipDetector, ChipVendor, VENDOR_NAMES, sanitize_docker_tag


@dataclass
class VerifyConfig:
    """验证阶段配置"""
    enabled: bool = True
    # 是否下载权重
    download_weights: bool = True
    weights_source: str = ""  # 权重来源 URL 或路径
    weights_local_path: str = ""  # 权重本地存储路径
    # 容器相关
    start_container: bool = True
    enter_container: bool = True
    container_name: str = "flagos"
    image_path: str = ""  # 镜像路径
    container_run_cmd: str = ""  # 启动容器命令
    # 服务相关
    start_service: bool = True
    serve_start_cmd: str = ""  # 启动服务命令
    # API 验证
    verify_api: bool = True
    api_endpoint: str = "http://localhost:8000/v1"
    api_verify_cmd: str = ""  # 自定义 API 验证命令
    api_timeout: int = 60  # API 验证超时时间（秒）


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
    input_type: str = "image"  # "image" 或 "container"
    container_name: str = ""
    image_path: str = ""

    # 各阶段配置
    verify: VerifyConfig = field(default_factory=VerifyConfig)
    chip: ChipConfig = field(default_factory=ChipConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    model_info: ModelInfo = field(default_factory=ModelInfo)

    # 执行哪些阶段
    stages_to_run: List[str] = field(default_factory=lambda: ["verify", "publish"])


def load_config(config_path: str) -> PipelineConfig:
    """从 YAML 文件加载配置"""
    with open(config_path, 'r', encoding='utf-8') as f:
        raw_config = yaml.safe_load(f)

    config = PipelineConfig()

    # 基本配置
    config.input_type = raw_config.get('input_type', 'image')
    config.container_name = raw_config.get('container_name', '')
    config.image_path = raw_config.get('image_path', '')
    config.stages_to_run = raw_config.get('stages_to_run', ['verify', 'publish'])

    # 验证阶段配置
    if 'verify' in raw_config:
        v = raw_config['verify']
        config.verify = VerifyConfig(
            enabled=v.get('enabled', True),
            download_weights=v.get('download_weights', True),
            weights_source=v.get('weights_source', ''),
            weights_local_path=v.get('weights_local_path', ''),
            start_container=v.get('start_container', True),
            enter_container=v.get('enter_container', True),
            container_name=v.get('container_name', 'flagos'),
            image_path=v.get('image_path', ''),
            container_run_cmd=v.get('container_run_cmd', ''),
            start_service=v.get('start_service', True),
            serve_start_cmd=v.get('serve_start_cmd', ''),
            verify_api=v.get('verify_api', True),
            api_endpoint=v.get('api_endpoint', 'http://localhost:8000/v1'),
            api_verify_cmd=v.get('api_verify_cmd', ''),
            api_timeout=v.get('api_timeout', 60)
        )

    # 芯片配置
    if 'chip' in raw_config:
        c = raw_config['chip']
        config.chip = ChipConfig(
            vendor=c.get('vendor', 'auto'),
            auto_generate_tag=c.get('auto_generate_tag', True),
            harbor_registry=c.get('harbor_registry', 'harbor.baai.ac.cn/flagrelease-public'),
            tree=c.get('tree', 'none'),
            gems_version=c.get('gems_version', ''),
            scale_version=c.get('scale_version', ''),
            cx=c.get('cx', 'none'),
            date_tag=c.get('date_tag', ''),
            driver_version=c.get('driver_version', ''),
            sdk_version=c.get('sdk_version', ''),
            torch_version=c.get('torch_version', ''),
            python_version=c.get('python_version', ''),
            gpu_model=c.get('gpu_model', '')
        )

    # 发布阶段配置
    if 'publish' in raw_config:
        p = raw_config['publish']
        config.publish = PublishConfig(
            enabled=p.get('enabled', True),
            tag_image=p.get('tag_image', True),
            push_harbor=p.get('push_harbor', True),
            generate_readme=p.get('generate_readme', True),
            readme_output_path=p.get('readme_output_path', './README.md'),
            publish_modelscope=p.get('publish_modelscope', True),
            modelscope_model_id=p.get('modelscope_model_id', ''),
            modelscope_token=p.get('modelscope_token', os.environ.get('MODELSCOPE_TOKEN', '')),
            publish_huggingface=p.get('publish_huggingface', True),
            huggingface_repo_id=p.get('huggingface_repo_id', ''),
            huggingface_token=p.get('huggingface_token', os.environ.get('HF_TOKEN', '')),
            upload_weights=p.get('upload_weights', True),
            weights_dir=p.get('weights_dir', ''),
            private=p.get('private', True),
            existing_harbor_image=p.get('existing_harbor_image', ''),
            image_source=p.get('image_source', ''),
            image_target_tag=p.get('image_target_tag', ''),
            harbor_path=p.get('harbor_path', ''),
            readme_script_path=p.get('readme_script_path', ''),
            upload_files=p.get('upload_files', [])
        )

    # 模型信息配置
    if 'model_info' in raw_config:
        m = raw_config['model_info']
        config.model_info = ModelInfo(
            source_of_model_weights=m.get('source_of_model_weights', ''),
            new_model_introduction=m.get('new_model_introduction', ''),
            evaluation_results=m.get('evaluation_results', []),
            output_name=m.get('output_name', ''),
            vendor=m.get('vendor', ''),
            docker_version=m.get('docker_version', ''),
            ubuntu_version=m.get('ubuntu_version', ''),
            flagrelease_name=m.get('flagrelease_name', ''),
            flagrelease_name_pre=m.get('flagrelease_name_pre', ''),
            image_harbor_path=m.get('image_harbor_path', ''),
            container_run_cmd=m.get('container_run_cmd', ''),
            serve_start_cmd=m.get('serve_start_cmd', ''),
            serve_infer_cmd=m.get('serve_infer_cmd', ''),
        )

    # 根据 input_type 自动调整
    if config.input_type == 'container':
        config.verify.download_weights = False
        config.verify.start_container = False
        config.verify.enter_container = False
        config.verify.start_service = False

    return config


def validate_config(config: PipelineConfig) -> List[str]:
    """验证配置是否完整"""
    errors = []

    if config.input_type not in ['image', 'container']:
        errors.append(f"Invalid input_type: {config.input_type}. Must be 'image' or 'container'")

    if config.input_type == 'image' and not config.image_path:
        errors.append("image_path is required when input_type is 'image'")

    if config.input_type == 'container' and not config.container_name:
        errors.append("container_name is required when input_type is 'container'")

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


def auto_fill_config(config: PipelineConfig, container_name: Optional[str] = None) -> PipelineConfig:
    """根据环境检测自动填充配置中的空字段"""
    import datetime
    import re

    # 确定容器名称
    if config.input_type == 'container':
        container = config.container_name
    else:
        container = container_name or config.verify.container_name

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

    if not config.publish.image_source:
        if config.image_path:
            config.publish.image_source = config.image_path
        elif config.verify.image_path:
            config.publish.image_source = config.verify.image_path

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

    if not config.model_info.serve_start_cmd and config.verify.serve_start_cmd:
        config.model_info.serve_start_cmd = config.verify.serve_start_cmd

    if not config.model_info.serve_infer_cmd:
        api_endpoint = config.verify.api_endpoint or "http://localhost:8000/v1"
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
        if config.verify.weights_local_path:
            config.publish.weights_dir = config.verify.weights_local_path

    return config
