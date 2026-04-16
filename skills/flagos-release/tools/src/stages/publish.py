"""
发布阶段
包含：镜像打 tag、推送 Harbor、生成 README、发布到 ModelScope/HuggingFace
"""
import json
import os
import time
import subprocess
from typing import Optional, List, Tuple
from pathlib import Path

from .base import BaseStage, StageResult, StepResult, StepStatus
from ..chip_detector import ChipDetector, ChipVendor, EnvironmentInfo, generate_image_tag

# 上传重试配置
UPLOAD_MAX_RETRIES = 5
UPLOAD_RETRY_DELAY = 10
UPLOAD_MAX_DELAY = 300
UPLOAD_TIMEOUT = 3600


def format_file_size(size_bytes: int) -> str:
    """格式化文件大小"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.2f} PB"


def get_files_in_directory(directory: str, extensions: List[str] = None) -> List[str]:
    """获取目录中的所有文件"""
    if not os.path.exists(directory):
        return []
    files = []
    for root, _, filenames in os.walk(directory):
        for filename in filenames:
            file_path = os.path.join(root, filename)
            if extensions:
                if any(filename.endswith(ext) for ext in extensions):
                    files.append(file_path)
            else:
                files.append(file_path)
    return files


class PublishStage(BaseStage):
    """发布阶段"""

    def __init__(self, config):
        super().__init__(config)
        self.env_info: Optional[EnvironmentInfo] = None

    @property
    def name(self) -> str:
        return "发布阶段"

    def run(self) -> StageResult:
        """执行发布阶段"""
        print(f"\n{'='*60}")
        print(f"开始执行: {self.name}")
        print(f"{'='*60}")

        start_time = time.time()
        publish_config = self.config.publish
        harbor_failed = False

        # 如果已有 Harbor 镜像地址，跳过 commit/tag/push
        if publish_config.existing_harbor_image:
            existing_image = publish_config.existing_harbor_image
            print(f"  已配置 existing_harbor_image: {existing_image}")
            print(f"  跳过容器 commit、镜像打 tag、推送 Harbor 步骤")
            self.config.publish.harbor_path = existing_image
            self.config.model_info.image_harbor_path = existing_image
            self.skip_step("容器 commit", "已有 Harbor 镜像")
            self.skip_step("镜像打 tag", "已有 Harbor 镜像")
            self.skip_step("推送 Harbor", "已有 Harbor 镜像")
        else:
            # 0. 如果输入是容器，先 commit 为镜像
            if self.config.input_type == 'container':
                success = self._commit_container()
                if not success:
                    return self.make_result(False, "容器 commit 失败")

            # 1. 镜像打 tag
            if publish_config.tag_image:
                success = self._tag_image()
                if not success:
                    return self.make_result(False, "镜像打 tag 失败")
            else:
                self.skip_step("镜像打 tag", "配置跳过")

            # 2. 推送到 Harbor
            if publish_config.push_harbor:
                success = self._push_to_harbor()
                if not success:
                    harbor_failed = True
                    print("  ⚠ Harbor 推送失败，继续执行后续步骤（README 生成、数据回传）")
            else:
                self.skip_step("推送 Harbor", "配置跳过")

        # 3. 生成 README
        readme_path = None
        if publish_config.generate_readme:
            readme_path = self._generate_readme()
            if not readme_path:
                return self.make_result(False, "生成 README 失败")
        else:
            self.skip_step("生成 README", "配置跳过")

        # 4. 发布到 ModelScope
        ms_failed = False
        if publish_config.publish_modelscope:
            success = self._publish_to_modelscope(readme_path)
            if not success:
                ms_failed = True
                print("  ⚠ ModelScope 发布失败，继续执行 HuggingFace 上传")
        else:
            self.skip_step("发布到 ModelScope", "配置跳过")

        # 5. 发布到 HuggingFace
        hf_failed = False
        if publish_config.publish_huggingface:
            success = self._publish_to_huggingface(readme_path)
            if not success:
                hf_failed = True
                print("  ⚠ HuggingFace 发布失败")
        else:
            self.skip_step("发布到 HuggingFace", "配置跳过")

        # 6. 数据回传到宿主机
        self._sync_to_host()

        upload_failed = ms_failed or hf_failed
        duration = time.time() - start_time
        if harbor_failed or upload_failed:
            failures = []
            if harbor_failed:
                failures.append("Harbor")
            if ms_failed:
                failures.append("ModelScope")
            if hf_failed:
                failures.append("HuggingFace")
            print(f"\n⚠ {self.name} 完成，但部分平台失败: {', '.join(failures)} (总耗时 {duration:.2f}s)")
        else:
            print(f"\n+ {self.name} 完成 (总耗时 {duration:.2f}s)")
        return self.make_result(not harbor_failed and not upload_failed)

    def _sync_to_host(self):
        """将容器内 /flagos-workspace 的产出同步到宿主机工作目录。

        检查宿主机目标目录是否已有对应文件，缺失或大小不一致则 docker cp 回传。
        回传失败不影响整体流水线结果。
        """
        container_name = self.config.container_name
        host_base = self.config.host_workspace_base

        if not container_name or not host_base:
            self.skip_step("数据回传", "缺少容器名/宿主机路径")
            return

        # host_workspace_base 已包含完整路径（如 /data/flagos-workspace/Qwen/Qwen2.5-0.5B-Instruct）
        # 直接使用，不再拼接 model_source
        host_target = host_base
        print(f"\n[数据回传] 同步到宿主机: {host_target}")

        # 整目录 docker cp，确保子目录（如 results/outputs/...）也被同步
        sync_dirs = ["results", "traces", "logs"]
        synced = 0
        failed = 0

        for dir_name in sync_dirs:
            container_dir = f"/flagos-workspace/{dir_name}"
            host_dir = os.path.join(host_target, dir_name)
            os.makedirs(host_dir, exist_ok=True)

            try:
                cp_result = subprocess.run(
                    ["docker", "cp", f"{container_name}:{container_dir}/.", host_dir + "/"],
                    capture_output=True, text=True, timeout=120
                )
                if cp_result.returncode == 0:
                    print(f"  ✓ {dir_name}/ 已同步")
                    synced += 1
                else:
                    print(f"  ⚠ {dir_name}/ 同步失败: {cp_result.stderr.strip()}")
                    failed += 1
            except Exception as e:
                print(f"  ⚠ {dir_name}/ 同步异常: {e}")
                failed += 1

        # context.yaml 单独处理：回传时重命名为 context_snapshot.yaml
        config_dir = os.path.join(host_target, "config")
        os.makedirs(config_dir, exist_ok=True)
        try:
            cp_result = subprocess.run(
                ["docker", "cp",
                 f"{container_name}:/flagos-workspace/shared/context.yaml",
                 os.path.join(config_dir, "context_snapshot.yaml")],
                capture_output=True, text=True, timeout=30
            )
            if cp_result.returncode == 0:
                print(f"  ✓ context_snapshot.yaml 已同步")
                synced += 1
            else:
                print(f"  ⚠ context_snapshot.yaml 同步失败: {cp_result.stderr.strip()}")
                failed += 1
        except Exception as e:
            print(f"  ⚠ context_snapshot.yaml 同步异常: {e}")
            failed += 1

        summary = f"同步 {synced} 个目录/文件, 失败 {failed} 个"
        print(f"  {summary}")

        self.steps.append(StepResult(
            step_name="数据回传到宿主机",
            status=StepStatus.SUCCESS if failed == 0 else StepStatus.FAILED,
            message=summary
        ))

    def _commit_container(self) -> bool:
        """将容器 commit 为镜像"""
        container_name = self.config.container_name
        if not container_name:
            print("  x 容器名称未配置")
            return False

        model_name = self.config.model_info.output_name or "model"
        commit_image_name = f"flagrelease-commit-{container_name}:{model_name}".lower().replace("/", "-")

        print(f"  正在将容器 {container_name} commit 为镜像 {commit_image_name}...")

        cmd = f"docker commit {container_name} {commit_image_name}"
        success, stdout, stderr = self.run_command(
            cmd=cmd,
            step_name="容器 commit",
            timeout=600
        )

        if success:
            self.config.publish.image_source = commit_image_name
            print(f"  + 容器已 commit 为镜像: {commit_image_name}")

        return success

    def _tag_image(self) -> bool:
        """镜像打 tag"""
        publish_config = self.config.publish
        chip_config = self.config.chip

        source_image = publish_config.image_source
        if not source_image:
            print("  x 源镜像未配置")
            return False

        if chip_config.auto_generate_tag:
            target_tag = self._generate_auto_tag()
            if not target_tag:
                return False
        else:
            target_tag = publish_config.image_target_tag or publish_config.harbor_path

        if not target_tag:
            print("  x 目标 tag 未配置")
            return False

        self.config.publish.harbor_path = target_tag
        self.config.model_info.image_harbor_path = target_tag

        cmd = f"docker tag {source_image} {target_tag}"
        success, _, _ = self.run_command(
            cmd=cmd,
            step_name="镜像打 tag",
            timeout=60
        )

        if success:
            print(f"  生成的镜像 tag: {target_tag}")

        return success

    def _generate_auto_tag(self) -> Optional[str]:
        """自动生成镜像 tag"""
        chip_config = self.config.chip
        publish_config = self.config.publish

        print("  正在生成镜像 tag...")

        try:
            # 优先使用 auto_fill_config 已生成的 tag
            if publish_config.image_target_tag:
                print(f"  使用已生成的 tag: {publish_config.image_target_tag}")

                print(f"    芯片厂商: {chip_config.vendor}")
                print(f"    驱动版本: {chip_config.driver_version}")
                print(f"    SDK版本: {chip_config.sdk_version}")
                print(f"    PyTorch版本: {chip_config.torch_version}")
                print(f"    Python版本: {chip_config.python_version}")
                print(f"    GPU型号: {chip_config.gpu_model}")
                print(f"    FlagGems版本: {chip_config.gems_version}")
                print(f"    FlagScale版本: {chip_config.scale_version}")

                self.steps.append(StepResult(
                    step_name="自动生成 tag",
                    status=StepStatus.SUCCESS,
                    output=publish_config.image_target_tag,
                    duration=0.0
                ))
                return publish_config.image_target_tag

            # 如果 auto_fill_config 没有生成 tag，则在此处生成
            if chip_config.vendor == "auto":
                container_name = self.config.container_name
                detector = ChipDetector(container_name=container_name if container_name else None)
                vendor = detector.detect_vendor()
                if vendor is None:
                    print("  x 无法自动检测芯片厂商，请在配置中手动指定 chip.vendor")
                    return None
            else:
                try:
                    vendor = ChipVendor(chip_config.vendor)
                except ValueError:
                    print(f"  x 未知的芯片厂商: {chip_config.vendor}")
                    return None

            from ..chip_detector import ChipVersionInfo, VENDOR_DETECT_INFO
            vendor_info = VENDOR_DETECT_INFO.get(vendor, {})
            chip_info = ChipVersionInfo(
                vendor=vendor,
                driver_version=chip_config.driver_version,
                sdk_version=chip_config.sdk_version,
                torch_backend=vendor_info.get("torch_backend", ""),
                torch_version=chip_config.torch_version,
                python_version=chip_config.python_version,
                gpu_model=chip_config.gpu_model,
                arch="amd64",
            )

            from ..config import _extract_model_name
            model_name = _extract_model_name(self.config.model_info.source_of_model_weights) or self.config.model_info.flagrelease_name_pre
            tag = generate_image_tag(
                info=chip_info,
                model_name=model_name,
                harbor_registry=chip_config.harbor_registry,
                tree=chip_config.tree,
                gems_version=chip_config.gems_version,
                scale_version=chip_config.scale_version,
                cx=chip_config.cx,
                date_tag=chip_config.date_tag
            )

            self.steps.append(StepResult(
                step_name="自动生成 tag",
                status=StepStatus.SUCCESS,
                output=tag,
                duration=0.0
            ))
            return tag

        except Exception as e:
            print(f"  x 自动生成 tag 失败: {e}")
            return None

    def _ensure_harbor_login(self, harbor_path: str) -> bool:
        """确保已登录 Harbor，环境变量存在时强制重新登录"""
        # 从 harbor_path 提取 registry 地址（如 harbor.baai.ac.cn）
        registry = harbor_path.split("/")[0]

        # 环境变量优先：有凭证就强制重新登录，避免复用旧凭证导致权限不匹配
        user = os.environ.get("HARBOR_USER", "")
        password = os.environ.get("HARBOR_PASSWORD", "")
        if user and password:
            print(f"  正在登录 Harbor: {registry} (使用环境变量凭证) ...")
            cmd = f'echo "{password}" | docker login --username={user} --password-stdin https://{registry}/'
            success, stdout, stderr = self.run_command(
                cmd=cmd,
                step_name="Harbor 登录",
                timeout=30,
            )
            if not success:
                print(f"  x Harbor 登录失败，请检查 HARBOR_USER / HARBOR_PASSWORD")
            return success

        # 无环境变量，检查是否已有登录凭证
        import json as _json
        docker_config_path = os.path.expanduser("~/.docker/config.json")
        if os.path.exists(docker_config_path):
            try:
                with open(docker_config_path) as f:
                    docker_config = _json.load(f)
                auths = docker_config.get("auths", {})
                if registry in auths or f"https://{registry}" in auths or f"https://{registry}/" in auths:
                    print(f"  Harbor 已登录: {registry} (使用已有凭证)")
                    return True
            except Exception:
                pass

        print(f"  x Harbor 未登录且环境变量 HARBOR_USER / HARBOR_PASSWORD 未设置")
        print(f"    请设置环境变量或手动执行: docker login https://{registry}/")
        self.steps.append(StepResult(
            step_name="Harbor 登录",
            status=StepStatus.FAILED,
            error="HARBOR_USER / HARBOR_PASSWORD 未设置",
        ))
        return False

    def _push_to_harbor(self) -> bool:
        """推送镜像到 Harbor"""
        publish_config = self.config.publish
        harbor_path = publish_config.harbor_path

        if not harbor_path:
            print("  x Harbor 路径未配置")
            return False

        # 确保已登录 Harbor
        if not self._ensure_harbor_login(harbor_path):
            return False

        cmd = f"docker push {harbor_path}"
        step_name = "推送 Harbor"
        timeout = 7200

        print(f"[{self.name}] 执行: {step_name}")
        print(f"  命令: {cmd}")

        start_time = time.time()
        try:
            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            output_lines = []
            for line in process.stdout:
                line = line.rstrip('\n')
                print(f"  {line}")
                output_lines.append(line)

            process.wait(timeout=timeout)
            duration = time.time() - start_time
            output = '\n'.join(output_lines)

            if process.returncode == 0:
                print(f"  + 成功 (耗时 {duration:.2f}s)")
                self.steps.append(StepResult(
                    step_name=step_name,
                    status=StepStatus.SUCCESS,
                    output=output,
                    duration=duration,
                ))
                return True
            else:
                error_msg = output or f"命令返回非零状态码: {process.returncode}"
                print(f"  x 失败: {error_msg[:200]}")
                self.steps.append(StepResult(
                    step_name=step_name,
                    status=StepStatus.FAILED,
                    output=output,
                    error=error_msg,
                    duration=duration,
                ))
                return False

        except subprocess.TimeoutExpired:
            process.kill()
            duration = time.time() - start_time
            error_msg = f"命令执行超时 ({timeout}秒)"
            print(f"  x 超时: {error_msg}")
            self.steps.append(StepResult(
                step_name=step_name,
                status=StepStatus.FAILED,
                error=error_msg,
                duration=duration,
            ))
            return False

        except Exception as e:
            duration = time.time() - start_time
            error_msg = str(e)
            print(f"  x 异常: {error_msg}")
            self.steps.append(StepResult(
                step_name=step_name,
                status=StepStatus.FAILED,
                error=error_msg,
                duration=duration,
            ))
            return False

    # ==================== README 生成 ====================

    def _generate_readme(self) -> Optional[str]:
        """生成 README"""
        publish_config = self.config.publish

        if publish_config.readme_script_path and os.path.exists(publish_config.readme_script_path):
            return self._generate_readme_by_script()

        return self._generate_readme_by_template()

    def _generate_readme_by_script(self) -> Optional[str]:
        """使用脚本生成 README"""
        publish_config = self.config.publish
        model_info = self.config.model_info

        import yaml
        import tempfile

        config_data = {
            "output_name": model_info.output_name,
            "vendor": model_info.vendor,
            "docker_version": model_info.docker_version,
            "ubuntu_version": model_info.ubuntu_version,
            "source_of_model_weights": model_info.source_of_model_weights,
            "flagrelease_name": model_info.flagrelease_name,
            "flagrelease_name_pre": model_info.flagrelease_name_pre,
            "image_harbor_path": model_info.image_harbor_path,
            "container_run_cmd": model_info.container_run_cmd,
            "serve_start_cmd": model_info.serve_start_cmd,
            "serve_infer_cmd": model_info.serve_infer_cmd,
            "new_model_introduction": model_info.new_model_introduction,
            "evaluation_table": self._generate_evaluation_table(),
        }

        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            yaml.dump(config_data, f, allow_unicode=True)
            temp_config_path = f.name

        try:
            cmd = f"python {publish_config.readme_script_path} --config {temp_config_path} --output {publish_config.readme_output_path}"
            success, stdout, stderr = self.run_command(
                cmd=cmd,
                step_name="生成 README (脚本)",
                timeout=120
            )
            if success:
                return publish_config.readme_output_path
            return None
        finally:
            if os.path.exists(temp_config_path):
                os.remove(temp_config_path)

    def _generate_readme_by_template(self) -> Optional[str]:
        """使用模板生成 README"""
        model_info = self.config.model_info
        publish_config = self.config.publish
        chip_config = self.config.chip

        if self.env_info is None:
            container_name = self.config.container_name
            try:
                detector = ChipDetector(container_name=container_name if container_name else None)
                vendor = None
                if chip_config.vendor != "auto":
                    try:
                        vendor = ChipVendor(chip_config.vendor)
                    except ValueError:
                        pass
                self.env_info = detector.detect_environment(vendor)
            except Exception as e:
                print(f"  警告: 无法检测环境信息: {e}")

        # 查找模板文件
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
            "templates", "README_TEMPLATE.md"
        )

        if not os.path.exists(template_path):
            print(f"  警告: 模板文件不存在: {template_path}，使用内置模板")
            return self._generate_readme_builtin()

        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                template_content = f.read()
        except Exception as e:
            print(f"  警告: 无法读取模板文件: {e}，使用内置模板")
            return self._generate_readme_builtin()

        template_vars = self._prepare_template_vars()

        readme_content = template_content
        for key, value in template_vars.items():
            placeholder = "{{" + key + "}}"
            readme_content = readme_content.replace(placeholder, str(value))

        output_path = self._get_readme_output_path()
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(readme_content)

            print(f"  + README 已生成: {output_path}")
            self.steps.append(StepResult(
                step_name="生成 README",
                status=StepStatus.SUCCESS,
                output=output_path
            ))
            return output_path

        except Exception as e:
            print(f"  x 生成 README 失败: {e}")
            return None

    def _get_readme_output_path(self) -> str:
        """获取 README 输出路径"""
        flagrelease_name = self.config.model_info.flagrelease_name
        if not flagrelease_name:
            flagrelease_name = self.config.model_info.output_name or "model"
        return os.path.join("output", flagrelease_name, "README.md")

    def _get_upload_directory(self, readme_path: Optional[str] = None) -> str:
        """获取上传目录"""
        publish_config = self.config.publish
        readme_output_path = self._get_readme_output_path()
        output_dir = os.path.dirname(readme_output_path)

        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # 如果启用了权重上传，将权重文件链接到 output 目录
        if publish_config.upload_weights and publish_config.weights_dir:
            weights_dir = publish_config.weights_dir
            if os.path.exists(weights_dir):
                # 宿主机上权重目录存在，直接链接
                print(f"  准备权重文件从: {weights_dir}")
                weight_files = get_files_in_directory(weights_dir)
                for wf in weight_files:
                    rel_path = os.path.relpath(wf, weights_dir)
                    dest_path = os.path.join(output_dir, rel_path)
                    dest_dir = os.path.dirname(dest_path)

                    if not os.path.exists(dest_dir):
                        os.makedirs(dest_dir, exist_ok=True)

                    if not os.path.exists(dest_path):
                        try:
                            os.symlink(os.path.abspath(wf), dest_path)
                        except OSError:
                            try:
                                os.link(wf, dest_path)
                            except OSError:
                                import shutil
                                shutil.copy2(wf, dest_path)

                print(f"    已准备 {len(weight_files)} 个权重文件")
            elif self.config.container_name:
                # 宿主机上不存在，尝试从容器 docker cp 权重到 output 目录
                # weights_dir 可能是 local_path（宿主机路径），容器内未必相同
                # 依次尝试 weights_dir 和 container_path
                container = self.config.container_name
                container_path = self.config.model_info.source_of_model_weights
                # 从 config 中获取容器内路径（通过 serve_start_cmd 中的模型路径推断）
                # 更直接：尝试 weights_dir，失败则用常见容器路径
                candidate_paths = [weights_dir]
                # 如果有 serve_start_cmd，从中提取容器内模型路径
                serve_cmd = self.config.model_info.serve_start_cmd or ""
                if "vllm serve " in serve_cmd:
                    parts = serve_cmd.split("vllm serve ", 1)[1].split()
                    if parts:
                        cmd_model_path = parts[0].strip().rstrip("\\")
                        if cmd_model_path != weights_dir:
                            candidate_paths.append(cmd_model_path)

                try:
                    print(f"  宿主机无权重目录 {weights_dir}，从容器 {container} 复制...")
                    copied = False
                    for cpath in candidate_paths:
                        try:
                            result = subprocess.run(
                                ["docker", "exec", container, "test", "-d", cpath],
                                capture_output=True, timeout=5
                            )
                            if result.returncode == 0:
                                cp_result = subprocess.run(
                                    ["docker", "cp", f"{container}:{cpath}/.", output_dir],
                                    capture_output=True, text=True, timeout=600
                                )
                                if cp_result.returncode == 0:
                                    n = len([f for f in os.listdir(output_dir) if f != "README.md"])
                                    print(f"    已从容器 {cpath} 复制 {n} 个权重文件")
                                    copied = True
                                    break
                        except Exception:
                            continue
                    if not copied:
                        print(f"    ⚠ 容器内未找到权重目录: {candidate_paths}")
                except Exception as e:
                    print(f"    ⚠ 从容器复制权重异常: {e}")

        return output_dir

    def _prepare_template_vars(self) -> dict:
        """准备模板变量"""
        model_info = self.config.model_info
        chip_config = self.config.chip

        vars = {}

        vars["flagrelease_name"] = model_info.flagrelease_name or model_info.output_name
        vars["output_name"] = model_info.output_name
        vars["source_of_model_weights"] = model_info.source_of_model_weights

        if self.env_info and self.env_info.vendor:
            vars["vendor"] = self.env_info.vendor.value
            vars["vendor_cn_name"] = self.env_info.vendor_cn_name
        else:
            vars["vendor"] = model_info.vendor.lower() if model_info.vendor else "unknown"
            vars["vendor_cn_name"] = model_info.vendor or "Unknown"

        if self.env_info:
            vars["driver_version"] = self.env_info.driver_version or "N/A"
            vars["docker_version"] = self.env_info.docker_version or model_info.docker_version or "N/A"
            vars["os_info"] = f"{self.env_info.os_name} {self.env_info.os_version}".strip() or model_info.ubuntu_version or "Linux"
            vars["kernel_version"] = self.env_info.kernel_version or "N/A"
            vars["sdk_name"] = self.env_info.sdk_name or ""
            vars["sdk_version"] = self.env_info.sdk_version or "N/A"
            vars["gpu_model"] = self.env_info.gpu_model or "N/A"
            vars["python_version"] = self.env_info.python_version or "N/A"
            vars["torch_version"] = self.env_info.torch_version or "N/A"
            vars["torch_backend"] = self.env_info.torch_backend or "N/A"
            vars["flagscale_version"] = self.env_info.flagscale_version or chip_config.scale_version or "N/A"
            vars["flaggems_version"] = self.env_info.flaggems_version or chip_config.gems_version or "N/A"
            if self.env_info.vllm_version:
                vars["vllm_row"] = f"| vLLM | Version: {self.env_info.vllm_version} |"
            else:
                vars["vllm_row"] = ""
        else:
            vars["driver_version"] = "N/A"
            vars["docker_version"] = model_info.docker_version or "N/A"
            vars["os_info"] = model_info.ubuntu_version or "Linux"
            vars["kernel_version"] = "N/A"
            vars["sdk_name"] = ""
            vars["sdk_version"] = "N/A"
            vars["gpu_model"] = "N/A"
            vars["python_version"] = "N/A"
            vars["torch_version"] = "N/A"
            vars["torch_backend"] = "N/A"
            vars["flagscale_version"] = chip_config.scale_version or "N/A"
            vars["flaggems_version"] = chip_config.gems_version or "N/A"
            vars["vllm_row"] = ""

        vars["image_harbor_path"] = model_info.image_harbor_path or self.config.publish.harbor_path or "N/A"
        vars["weights_local_path"] = self.config.publish.weights_dir or "/data/models/" + (model_info.source_of_model_weights.split("/")[-1] if model_info.source_of_model_weights else "model")

        vars["container_run_cmd"] = model_info.container_run_cmd.strip() if model_info.container_run_cmd else "# 请在配置文件的 model_info.container_run_cmd 中填写容器启动命令"
        vars["serve_start_cmd"] = model_info.serve_start_cmd.strip() if model_info.serve_start_cmd else "# Serve start command"
        vars["serve_infer_cmd"] = model_info.serve_infer_cmd.strip() if model_info.serve_infer_cmd else "# Inference code"

        vars["evaluation_table"] = self._generate_evaluation_table()

        return vars

    def _generate_evaluation_table(self) -> str:
        """从 evaluation_results 或 results_dir 自动生成 Markdown 表格"""
        # 优先使用配置中手动填写的 evaluation_results
        results = self.config.model_info.evaluation_results
        if not results:
            # 尝试从 results_dir 自动读取
            results = self._load_results_from_dir()
        if not results:
            return "| Metrics | Origin | FlagOS |\n|---------|--------|--------|\n| N/A | N/A | N/A |"

        table = "| Metrics | Origin | FlagOS |\n|---------|--------|--------|\n"
        for item in results:
            metric = item.get('metric', 'N/A')
            origin = item.get('origin', 'N/A')
            flagos = item.get('flagos', 'N/A')
            table += f"| {metric} | {origin} | {flagos} |\n"

        return table.strip()

    def _load_results_from_dir(self) -> List[dict]:
        """从 results_dir 自动读取精度评测结果，返回兼容 evaluation_results 的格式"""
        results_dir = self.config.publish.results_dir
        if not results_dir or not os.path.isdir(results_dir):
            return []

        results = []

        # 读取 GPQA 精度结果
        gpqa_native_path = os.path.join(results_dir, "gpqa_native.json")
        gpqa_flagos_path = os.path.join(results_dir, "gpqa_flagos.json")

        native_score = self._read_json_field(gpqa_native_path, "score")
        flagos_score = self._read_json_field(gpqa_flagos_path, "score")

        if native_score is not None or flagos_score is not None:
            results.append({
                "metric": "GPQA",
                "origin": native_score if native_score is not None else "N/A",
                "flagos": flagos_score if flagos_score is not None else "N/A",
            })

        return results

    @staticmethod
    def _read_json_field(filepath: str, field: str):
        """安全读取 JSON 文件中的某个字段"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data.get(field)
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            return None

    def _generate_readme_builtin(self) -> Optional[str]:
        """使用内置简单模板生成 README"""
        model_info = self.config.model_info

        env_table = self._build_environment_table()
        eval_table = self._generate_evaluation_table()

        container_run_cmd = model_info.container_run_cmd or "# 请在配置文件的 model_info.container_run_cmd 中填写容器启动命令"
        serve_start_cmd = model_info.serve_start_cmd or "# Serve start command"
        serve_infer_cmd = model_info.serve_infer_cmd or "# Inference code"

        readme_content = f"""# {model_info.flagrelease_name}

## Model Information

| Item | Value |
|------|-------|
| Model Name | {model_info.output_name} |
| Chip Vendor | {model_info.vendor} |
| Source | {model_info.source_of_model_weights} |

## Test Environment

{env_table}

## Quick Start

### 1. Pull and Run Container

```bash
{container_run_cmd}
```

### 2. Start Service

```bash
{serve_start_cmd}
```

### 3. Inference Example

```python
{serve_infer_cmd}
```

## Evaluation Results

{eval_table}

## Docker Image

```
{model_info.image_harbor_path}
```

---

*This README was auto-generated by FlagRelease*
"""

        output_path = self._get_readme_output_path()
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        try:
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(readme_content)

            print(f"  + README 已生成: {output_path}")
            self.steps.append(StepResult(
                step_name="生成 README",
                status=StepStatus.SUCCESS,
                output=output_path
            ))
            return output_path

        except Exception as e:
            print(f"  x 生成 README 失败: {e}")
            return None

    def _build_environment_table(self) -> str:
        """构建环境信息表格"""
        model_info = self.config.model_info
        chip_config = self.config.chip

        rows = []

        if self.env_info and self.env_info.os_name:
            os_info = f"{self.env_info.os_name} {self.env_info.os_version}".strip()
        else:
            os_info = model_info.ubuntu_version or "N/A"
        rows.append(("Operating System", os_info))

        if self.env_info and self.env_info.kernel_version:
            rows.append(("Kernel Version", self.env_info.kernel_version))

        if self.env_info and self.env_info.docker_version:
            docker_ver = self.env_info.docker_version
        else:
            docker_ver = model_info.docker_version or "N/A"
        rows.append(("Docker Version", docker_ver))

        if self.env_info and self.env_info.vendor:
            vendor_info = f"{self.env_info.vendor_cn_name} ({self.env_info.vendor.value})"
        else:
            vendor_info = model_info.vendor or "N/A"
        rows.append(("Chip Vendor", vendor_info))

        if self.env_info and self.env_info.driver_version:
            rows.append(("Driver Version", self.env_info.driver_version))

        if self.env_info and self.env_info.sdk_version:
            sdk_info = f"{self.env_info.sdk_name} {self.env_info.sdk_version}" if self.env_info.sdk_name else self.env_info.sdk_version
            rows.append(("SDK Version", sdk_info))

        if self.env_info and self.env_info.gpu_model:
            rows.append(("GPU Model", self.env_info.gpu_model))

        if self.env_info and self.env_info.gpu_count > 0:
            rows.append(("GPU Count", str(self.env_info.gpu_count)))

        if self.env_info and self.env_info.python_version:
            rows.append(("Python Version", self.env_info.python_version))

        if self.env_info and self.env_info.torch_version:
            torch_info = f"{self.env_info.torch_version} ({self.env_info.torch_backend})" if self.env_info.torch_backend else self.env_info.torch_version
            rows.append(("PyTorch Version", torch_info))

        if self.env_info and self.env_info.flaggems_version:
            rows.append(("FlagGems Version", self.env_info.flaggems_version))
        elif chip_config.gems_version:
            rows.append(("FlagGems Version", chip_config.gems_version))

        if self.env_info and self.env_info.flagscale_version:
            rows.append(("FlagScale Version", self.env_info.flagscale_version))
        elif chip_config.scale_version:
            rows.append(("FlagScale Version", chip_config.scale_version))

        if self.env_info and self.env_info.vllm_version:
            rows.append(("vLLM Version", self.env_info.vllm_version))

        if self.env_info and self.env_info.arch:
            rows.append(("Architecture", self.env_info.arch))

        table = "| Item | Value |\n|------|-------|\n"
        for item, value in rows:
            table += f"| {item} | {value} |\n"

        return table

    # ==================== ModelScope ====================

    def _publish_to_modelscope(self, readme_path: Optional[str]) -> bool:
        """发布到 ModelScope（SDK 优先，CLI 降级）"""
        publish_config = self.config.publish

        model_name = self.config.model_info.flagrelease_name or self.config.model_info.output_name
        model_id = publish_config.modelscope_model_id or f"FlagRelease/{model_name}"

        upload_dir = self._get_upload_directory(readme_path)
        if not upload_dir or not os.path.exists(upload_dir):
            print(f"  x 上传目录不存在: {upload_dir}")
            return False

        files = get_files_in_directory(upload_dir)
        total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        print(f"  准备上传目录: {upload_dir}")
        print(f"  文件数量: {len(files)}, 总大小: {format_file_size(total_size)}")
        print(f"  目标仓库: {model_id}")
        print(f"  可见性: {'私有' if publish_config.private else '公开'}")

        try:
            from modelscope.hub.api import HubApi

            api = HubApi()
            if publish_config.modelscope_token:
                api.login(publish_config.modelscope_token)

            print(f"  检查 ModelScope 模型仓库: {model_id}")
            try:
                api.get_model(model_id)
                print(f"    仓库已存在")
            except Exception:
                print(f"    仓库不存在，创建中...")
                try:
                    visibility = 1 if publish_config.private else 3
                    api.create_model(model_id=model_id, visibility=visibility)
                    print(f"    + 仓库创建成功 ({'私有' if publish_config.private else '公开'})")
                except Exception as e:
                    print(f"    创建仓库失败: {e}，继续尝试上传...")

            print(f"  开始上传...")
            api.upload_folder(repo_id=model_id, folder_path=upload_dir)

            print(f"  + 已发布到 ModelScope: {model_id}")
            self.steps.append(StepResult(
                step_name="发布到 ModelScope",
                status=StepStatus.SUCCESS,
                output=f"https://modelscope.cn/models/{model_id}"
            ))
            return True

        except ImportError:
            print("  modelscope SDK 未安装，尝试使用命令行方式...")
            return self._publish_to_modelscope_cli(readme_path)

        except Exception as e:
            print(f"  x 发布到 ModelScope 失败: {e}")
            return False

    def _publish_to_modelscope_cli(self, readme_path: Optional[str]) -> bool:
        """使用命令行发布到 ModelScope"""
        publish_config = self.config.publish

        if publish_config.modelscope_token:
            os.environ['MODELSCOPE_API_TOKEN'] = publish_config.modelscope_token

        model_name = self.config.model_info.flagrelease_name or self.config.model_info.output_name
        model_id = publish_config.modelscope_model_id or f"FlagRelease/{model_name}"

        upload_dir = self._get_upload_directory(readme_path)
        if not upload_dir or not os.path.exists(upload_dir):
            print(f"  x 上传目录不存在: {upload_dir}")
            return False

        files = get_files_in_directory(upload_dir)
        total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        print(f"  准备上传目录: {upload_dir}")
        print(f"  文件数量: {len(files)}, 总大小: {format_file_size(total_size)}")
        print(f"  目标仓库: {model_id}")

        cmd = f"modelscope upload {model_id} {upload_dir}"

        success = False
        current_delay = UPLOAD_RETRY_DELAY

        for attempt in range(UPLOAD_MAX_RETRIES):
            result, stdout, stderr = self.run_command(
                cmd=cmd,
                step_name="上传到 ModelScope",
                timeout=UPLOAD_TIMEOUT
            )
            if result:
                success = True
                print(f"  + 已发布到 ModelScope: {model_id}")
                self.steps.append(StepResult(
                    step_name="发布到 ModelScope",
                    status=StepStatus.SUCCESS,
                    output=f"https://modelscope.cn/models/{model_id}"
                ))
                break
            else:
                if attempt < UPLOAD_MAX_RETRIES - 1:
                    print(f"  x 上传失败 (尝试 {attempt+1}/{UPLOAD_MAX_RETRIES})")
                    print(f"  等待 {current_delay} 秒后重试...")
                    time.sleep(current_delay)
                    current_delay = min(current_delay * 2, UPLOAD_MAX_DELAY)
                else:
                    print(f"  x 上传失败，已达最大重试次数")

        return success

    # ==================== HuggingFace ====================

    def _publish_to_huggingface(self, readme_path: Optional[str]) -> bool:
        """发布到 HuggingFace（SDK 优先，CLI 降级）"""
        publish_config = self.config.publish

        model_name = self.config.model_info.flagrelease_name or self.config.model_info.output_name
        repo_id = publish_config.huggingface_repo_id or f"FlagRelease/{model_name}"

        upload_dir = self._get_upload_directory(readme_path)
        if not upload_dir or not os.path.exists(upload_dir):
            print(f"  x 上传目录不存在: {upload_dir}")
            return False

        files = get_files_in_directory(upload_dir)
        total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        print(f"  准备上传目录: {upload_dir}")
        print(f"  文件数量: {len(files)}, 总大小: {format_file_size(total_size)}")
        print(f"  目标仓库: {repo_id}")
        print(f"  可见性: {'私有' if publish_config.private else '公开'}")

        # 默认使用 hf-mirror 镜像站，避免国内网络直连 huggingface.co 不可达
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            print(f"  HF_ENDPOINT 未设置，使用镜像站: https://hf-mirror.com")

        try:
            from huggingface_hub import HfApi, login

            if publish_config.huggingface_token:
                login(token=publish_config.huggingface_token)

            api = HfApi()

            print(f"  检查 HuggingFace 仓库: {repo_id}")
            try:
                api.repo_info(repo_id=repo_id)
                print(f"    仓库已存在")
            except Exception:
                print(f"    仓库不存在，创建中...")
                api.create_repo(
                    repo_id=repo_id,
                    private=publish_config.private,
                    exist_ok=True
                )
                print(f"    + 仓库创建成功 ({'私有' if publish_config.private else '公开'})")

            print(f"  开始上传...")
            api.upload_folder(repo_id=repo_id, folder_path=upload_dir)

            print(f"  + 已发布到 HuggingFace: {repo_id}")
            self.steps.append(StepResult(
                step_name="发布到 HuggingFace",
                status=StepStatus.SUCCESS,
                output=f"https://huggingface.co/{repo_id}"
            ))
            return True

        except ImportError:
            print("  huggingface_hub SDK 未安装，尝试使用命令行方式...")
            return self._publish_to_huggingface_cli(readme_path)

        except Exception as e:
            print(f"  x 发布到 HuggingFace 失败: {e}")
            return False

    def _publish_to_huggingface_cli(self, readme_path: Optional[str]) -> bool:
        """使用命令行发布到 HuggingFace"""
        publish_config = self.config.publish

        if publish_config.huggingface_token:
            # 通过环境变量传递 token，避免在命令行参数中暴露
            os.environ['HF_TOKEN'] = publish_config.huggingface_token
            login_cmd = "huggingface-cli login --token $HF_TOKEN"
            success, _, _ = self.run_command(
                cmd=login_cmd,
                step_name="HuggingFace 登录",
                timeout=60
            )
            if not success:
                return False

        model_name = self.config.model_info.flagrelease_name or self.config.model_info.output_name
        repo_id = publish_config.huggingface_repo_id or f"FlagRelease/{model_name}"

        upload_dir = self._get_upload_directory(readme_path)
        if not upload_dir or not os.path.exists(upload_dir):
            print(f"  x 上传目录不存在: {upload_dir}")
            return False

        files = get_files_in_directory(upload_dir)
        total_size = sum(os.path.getsize(f) for f in files if os.path.exists(f))
        print(f"  准备上传目录: {upload_dir}")
        print(f"  文件数量: {len(files)}, 总大小: {format_file_size(total_size)}")
        print(f"  目标仓库: {repo_id}")

        private_flag = "--private" if publish_config.private else ""
        cmd = f"huggingface-cli upload {repo_id} {upload_dir} {private_flag}".strip()

        success = False
        current_delay = UPLOAD_RETRY_DELAY

        for attempt in range(UPLOAD_MAX_RETRIES):
            result, stdout, stderr = self.run_command(
                cmd=cmd,
                step_name="上传到 HuggingFace",
                timeout=UPLOAD_TIMEOUT
            )
            if result:
                success = True
                print(f"  + 已发布到 HuggingFace: {repo_id}")
                self.steps.append(StepResult(
                    step_name="发布到 HuggingFace",
                    status=StepStatus.SUCCESS,
                    output=f"https://huggingface.co/{repo_id}"
                ))
                break
            else:
                if attempt < UPLOAD_MAX_RETRIES - 1:
                    print(f"  x 上传失败 (尝试 {attempt+1}/{UPLOAD_MAX_RETRIES})")
                    print(f"    等待 {current_delay} 秒后重试...")
                    time.sleep(current_delay)
                    current_delay = min(current_delay * 2, UPLOAD_MAX_DELAY)
                else:
                    print(f"  x 上传失败，已达最大重试次数")

        return success
