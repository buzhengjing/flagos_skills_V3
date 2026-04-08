"""
验证阶段
包含：下载权重、启动容器、进入容器、启动服务、API验证
"""
import time
import requests

from .base import BaseStage, StageResult, StepResult, StepStatus


class VerifyStage(BaseStage):
    """验证阶段"""

    @property
    def name(self) -> str:
        return "验证阶段"

    def run(self) -> StageResult:
        """执行验证阶段"""
        print(f"\n{'='*60}")
        print(f"开始执行: {self.name}")
        print(f"{'='*60}")

        start_time = time.time()
        verify_config = self.config.verify

        # 1. 下载权重
        if verify_config.download_weights:
            success = self._download_weights()
            if not success:
                return self.make_result(False, "下载权重失败")
        else:
            self.skip_step("下载权重", "输入类型为容器或配置跳过")

        # 2. 启动容器
        if verify_config.start_container:
            success = self._start_container()
            if not success:
                return self.make_result(False, "启动容器失败")
        else:
            self.skip_step("启动容器", "输入类型为容器或配置跳过")

        # 3. 进入容器
        if verify_config.enter_container:
            self.skip_step("进入容器", "通过 docker exec 执行命令")
        else:
            self.skip_step("进入容器", "输入类型为容器或配置跳过")

        # 4. 启动服务
        if verify_config.start_service:
            success = self._start_service()
            if not success:
                return self.make_result(False, "启动服务失败")
        else:
            self.skip_step("启动服务", "输入类型为容器或配置跳过")

        # 5. 验证 API
        if verify_config.verify_api:
            success = self._verify_api()
            if not success:
                return self.make_result(False, "API 验证失败")
        else:
            self.skip_step("验证 API", "配置跳过")

        duration = time.time() - start_time
        print(f"\n+ {self.name} 完成 (总耗时 {duration:.2f}s)")
        return self.make_result(True)

    def _download_weights(self) -> bool:
        """下载权重"""
        verify_config = self.config.verify

        if not verify_config.weights_source:
            print("  警告: weights_source 未配置，跳过下载")
            self.skip_step("下载权重", "weights_source 未配置")
            return True

        weights_source = verify_config.weights_source
        local_path = verify_config.weights_local_path or "/data/models"

        if weights_source.startswith("http"):
            cmd = f"wget -c -P {local_path} {weights_source}"
        elif "/" in weights_source and not weights_source.startswith("/"):
            if "modelscope" in weights_source.lower():
                cmd = f"modelscope download --model {weights_source} --local_dir {local_path}"
            else:
                cmd = f"huggingface-cli download {weights_source} --local-dir {local_path}"
        else:
            cmd = f"ls -la {weights_source}"

        success, stdout, stderr = self.run_command(
            cmd=cmd,
            step_name="下载权重",
            timeout=3600
        )
        return success

    def _start_container(self) -> bool:
        """启动容器"""
        verify_config = self.config.verify
        container_name = verify_config.container_name

        check_cmd = f"docker ps -a --filter name=^{container_name}$ --format '{{{{.Names}}}}'"
        success, stdout, _ = self.run_command(
            cmd=check_cmd,
            step_name="检查容器状态",
            check=False
        )

        if stdout.strip() == container_name:
            status_cmd = f"docker ps --filter name=^{container_name}$ --format '{{{{.Status}}}}'"
            _, status_out, _ = self.run_command(
                cmd=status_cmd,
                step_name="检查容器运行状态",
                check=False
            )

            if status_out.strip():
                print(f"  容器 {container_name} 已在运行")
                self.skip_step("启动容器", "容器已在运行")
                return True
            else:
                start_cmd = f"docker start {container_name}"
                success, _, _ = self.run_command(
                    cmd=start_cmd,
                    step_name="启动已有容器"
                )
                return success

        run_cmd = verify_config.container_run_cmd
        if not run_cmd:
            image_path = verify_config.image_path or self.config.image_path
            if not image_path:
                print("  x 镜像路径未配置")
                return False
            run_cmd = f"docker run -itd --name {container_name} --network host {image_path}"

        success, stdout, stderr = self.run_command(
            cmd=run_cmd,
            step_name="启动容器",
            timeout=300
        )

        if success:
            time.sleep(5)

        return success

    def _start_service(self) -> bool:
        """启动服务"""
        verify_config = self.config.verify
        serve_cmd = verify_config.serve_start_cmd

        if not serve_cmd:
            print("  警告: serve_start_cmd 未配置")
            self.skip_step("启动服务", "serve_start_cmd 未配置")
            return True

        container_name = verify_config.container_name
        bg_cmd = f"docker exec -d {container_name} bash -c 'nohup {serve_cmd} > /tmp/serve.log 2>&1 &'"

        success, _, _ = self.run_command(
            cmd=bg_cmd,
            step_name="启动服务",
            timeout=60
        )

        if success:
            print("  等待服务启动 (30秒)...")
            time.sleep(30)

            check_cmd = f"docker exec {container_name} bash -c 'pgrep -f vllm || pgrep -f serve'"
            check_success, stdout, _ = self.run_command(
                cmd=check_cmd,
                step_name="检查服务进程",
                check=False
            )

            if not stdout.strip():
                log_cmd = f"docker exec {container_name} cat /tmp/serve.log"
                _, log_out, _ = self.run_command(
                    cmd=log_cmd,
                    step_name="查看服务日志",
                    check=False
                )
                print(f"  服务日志:\n{log_out[:1000]}")

        return success

    def _verify_api(self) -> bool:
        """验证 API"""
        verify_config = self.config.verify
        api_endpoint = verify_config.api_endpoint

        if verify_config.api_verify_cmd:
            success, _, _ = self.run_command(
                cmd=verify_config.api_verify_cmd,
                step_name="验证 API (自定义命令)",
                timeout=verify_config.api_timeout
            )
            return success

        print(f"  验证 API: {api_endpoint}")

        max_retries = 5
        retry_interval = 10

        for i in range(max_retries):
            try:
                base = api_endpoint.rstrip('/')
                if base.endswith('/v1'):
                    models_url = f"{base}/models"
                else:
                    models_url = f"{base}/v1/models"
                response = requests.get(models_url, timeout=30, proxies={"http": None, "https": None})

                if response.status_code == 200:
                    models = response.json()
                    print(f"  + API 可用，模型列表: {models}")
                    self.steps.append(StepResult(
                        step_name="验证 API",
                        status=StepStatus.SUCCESS,
                        output=f"Models: {models}",
                        duration=0.0
                    ))
                    return True
                else:
                    print(f"  重试 {i+1}/{max_retries}: API 返回 {response.status_code}")

            except requests.exceptions.ConnectionError:
                print(f"  重试 {i+1}/{max_retries}: 连接失败")
            except requests.exceptions.Timeout:
                print(f"  重试 {i+1}/{max_retries}: 请求超时")
            except Exception as e:
                print(f"  重试 {i+1}/{max_retries}: {e}")

            if i < max_retries - 1:
                time.sleep(retry_interval)

        print(f"  x API 验证失败，尝试 {max_retries} 次后仍无法连接 {api_endpoint}")
        return False
