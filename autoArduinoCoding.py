# -*- coding: utf-8 -*-
"""
文字描述驅動的Arduino自動程式生成系統
用戶透過文字描述硬體配置和邏輯，LLM自動生成、驗證、修復並部署程式碼。

執行前置作業:
1. 安裝必要的 Python 函式庫:
   pip install "fastapi[all]" requests pyserial
2. 設定環境變數:
   - Windows: set GOOGLE_API_KEY=您的API金鑰
   - macOS/Linux: export GOOGLE_API_KEY=您的API金鑰
3. 安裝 Arduino CLI:
   請參考官方文件安裝: https://arduino.github.io/arduino-cli/latest/installation/
"""

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
import json
import subprocess
import asyncio
import os
import tempfile
from datetime import datetime
import serial.tools.list_ports
from typing import List, Dict, Any, Tuple
import requests
import re
import shutil

# --------------------------------------------------------------------------
# FastAPI App Initialization
# --------------------------------------------------------------------------
app = FastAPI(title="文字驅動Arduino自動程式生成系統")

# --- 靜態檔案服務 (如果需要) ---
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")


# --------------------------------------------------------------------------
# Main System Class
# --------------------------------------------------------------------------
class TextDrivenArduinoSystem:
    """主系統邏輯，封裝了所有核心功能"""

    def __init__(self):
        """初始化系統，尋找CLI路徑並讀取API金鑰。"""
        self.user_description: str | None = None
        self.arduino_cli_path: str | None = self.find_arduino_cli()
        self.google_api_key: str | None = os.getenv("GOOGLE_API_KEY")
        self._cli_env_setup_done = False # 用於標記環境是否已設定

    def find_arduino_cli(self) -> str | None:
        """使用 shutil.which 更可靠地尋找 Arduino CLI 執行檔。"""
        possible_names = ["arduino-cli", "arduino-cli.exe"]
        for name in possible_names:
            path = shutil.which(name)
            if path:
                try:
                    subprocess.run([path, "version"], capture_output=True, text=True, timeout=10, check=True)
                    print(f"✅ 找到可正常執行的 Arduino CLI: {path}")
                    return path
                except Exception as e:
                    print(f"   - 找到路徑 {path} 但無法執行: {e}")
                    continue
        print("⚠️ 警告: 找不到 Arduino CLI。編譯與部署功能將無法使用。")
        return None

    def detect_arduino_devices(self) -> List[Dict[str, Any]]:
        """檢測連接的 Arduino 設備。"""
        devices = []
        try:
            ports = list(serial.tools.list_ports.comports())
        except Exception as e:
            print(f"❌ 掃描串列埠時發生錯誤: {e}")
            return []
        arduino_identifiers = ['Arduino', 'CH340', 'CP210x', 'FTDI', 'USB-SERIAL', 'Seeeduino', 'XIAO', 'ESP32']
        arduino_vid_pids = ["2341", "1A86", "10C4", "0403", "2886", "303A"]
        print(f"🔍 掃描到 {len(ports)} 個串列埠...")
        for port in ports:
            is_arduino = False
            if any(identifier.lower() in str(port.description).lower() or identifier.lower() in str(port.manufacturer).lower() for identifier in arduino_identifiers):
                is_arduino = True
            if port.vid and f"{port.vid:04X}".upper() in arduino_vid_pids:
                is_arduino = True
            devices.append({
                'port': port.device, 'description': port.description,
                'manufacturer': port.manufacturer,
                'vid_pid': f"{port.vid:04X}:{port.pid:04X}" if port.vid and port.pid else "N/A",
                'is_arduino': is_arduino
            })
        print(f"🎯 找到 {len([d for d in devices if d['is_arduino']])} 個可能的 Arduino 設備")
        return devices

    async def _call_gemini_api(self, prompt: str, is_json_output: bool = False) -> str | None:
        """通用的 Gemini API 呼叫函式，處理網路請求。"""
        if not self.google_api_key:
            raise ValueError("Google API key 未設定 (請設定環境變數 GOOGLE_API_KEY)")

        headers = {"Content-Type": "application/json"}
        generation_config = {"temperature": 0.2, "topP": 0.9, "maxOutputTokens": 8192}
        if is_json_output:
            generation_config["responseMimeType"] = "application/json"
            
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": generation_config}
        
        try:
            response = await asyncio.to_thread(
                requests.post,
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro-latest:generateContent?key={self.google_api_key}",
                headers=headers, json=payload, timeout=120
            )
            response.raise_for_status()
            result = response.json()
            return result['candidates'][0]['content']['parts'][0]['text']
        except requests.RequestException as e:
            print(f"❌ Gemini API 請求失敗: {e}")
            return None

    def _extract_code_from_json(self, text: str) -> dict | None:
        """從 AI 回應的 JSON 文本中提取內容。"""
        try:
            # Gemini 在 JSON 模式下有時會包含 Markdown 標籤，需要先移除
            clean_text = re.sub(r'```json\s*|\s*```', '', text, flags=re.DOTALL).strip()
            return json.loads(clean_text)
        except json.JSONDecodeError:
            print(f"❌ 解析 AI 的 JSON 回應失敗。原始文本: {text}")
            return None

    async def generate_code_and_instructions(self, description: str) -> dict | None:
        """【核心功能】【已升級】讓 AI 同時生成程式碼和接線指南。"""
        prompt = f"""作為一位資深的 Arduino 開發與教學專家，請根據以下用戶描述，生成一份包含 Arduino 程式碼和硬體接線指南的 JSON 物件。

### 用戶需求描述:
{description}

### 指示與規則:
1.  **智慧分配腳位**: 如果用戶**沒有**明確指定某個元件的腳位，你**必須**為 Seeeduino XIAO 選擇一個邏輯上合理且可用的腳位。
    * **可用腳位參考**:
        * 數位/類比: A0/D0, A1/D1, A2/D2, A3/D3
        * 僅數位: D4, D5, D6, D7, D8, D9, D10
    * 避免使用已被佔用的腳位。
2.  **生成接線指南**: 根據你最終決定的腳位分配（無論是使用者指定的還是你分配的），生成一份清晰、簡潔的 Markdown 格式接線指南。指南應包含每個元件的所有必要連接（如訊號、VCC、GND）。
3.  **生成非阻塞式程式碼**: 程式碼**必須**使用 `millis()` 來管理時間，**絕對禁止使用 `delay()`**，以確保多任務執行的流暢性。
4.  **輸出格式**: 你**必須**以一個 JSON 物件的形式回應，該物件包含兩個鍵：
    * `"arduino_code"`: (string) 完整的 Arduino .ino 程式碼。
    * `"wiring_instructions"`: (string) Markdown 格式的接線指南。

### JSON 輸出範例:
```json
{{
  "arduino_code": "#include <Servo.h>\\n\\nServo myServo;\\n\\nvoid setup() {{\\n  myServo.attach(A0);\\n}}\\n\\nvoid loop() {{\\n  myServo.write(90);\\n}}",
  "wiring_instructions": "### 接線指南\\n\\n**SG90 伺服馬達**\\n* **訊號線 (橘色)**: 連接到 Seeeduino XIAO 的 **A0** 腳位\\n* **VCC (紅色)**: 連接到 **5V** 腳位\\n* **GND (棕色)**: 連接到 **GND** 腳位"
}}
```

請開始生成 JSON 物件："""
        
        response_text = await self._call_gemini_api(prompt, is_json_output=True)
        return self._extract_code_from_json(response_text) if response_text else None

    async def setup_cli_environment(self, code: str, fqbn: str = "Seeeduino:samd:seeed_XIAO_m0"):
        """自動設定 Arduino CLI 環境，安裝核心和函式庫。"""
        if self._cli_env_setup_done or not self.arduino_cli_path:
            return

        print("🔧 正在設定並檢查 Arduino CLI 環境...")
        await asyncio.gather(
            self._run_cli_command("core", "update-index"),
            self._run_cli_command("core", "install", ":".join(fqbn.split(":")[:2]))
        )
        
        required_libs = re.findall(r'#include\s*<([^>]+)\.h>', code)
        if "Servo" not in required_libs and "Servo.h" not in code and "myServo" in code:
             required_libs.append("Servo")

        if required_libs:
            print(f"   - 程式碼需要函式庫: {set(required_libs)}")
            install_tasks = []
            for lib in set(required_libs): 
                lib_name_for_install = f'"{lib}"' if "Adafruit" in lib else lib
                install_tasks.append(self._run_cli_command("lib", "install", lib_name_for_install))
            await asyncio.gather(*install_tasks)
        
        self._cli_env_setup_done = True
        print("✅ CLI 環境設定完成。")

    async def _run_cli_command(self, *args):
        """執行一個 CLI 命令並等待它完成。"""
        if not self.arduino_cli_path: return
        proc = await asyncio.create_subprocess_exec(self.arduino_cli_path, *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await proc.wait()


    async def validate_and_fix_code(self, generation_result: dict, fqbn: str = "Seeeduino:samd:seeed_XIAO_m0") -> Tuple[bool, dict]:
        """驗證並在失敗時嘗試使用 AI 自動修復程式碼。"""
        if not self.arduino_cli_path:
            return True, generation_result
        
        code = generation_result.get("arduino_code", "")
        await self.setup_cli_environment(code, fqbn)

        for attempt in range(3):
            print(f"🔧 正在進行第 {attempt + 1} 次編譯嘗試...")
            success, result = await self._compile_code(code, fqbn)
            if success:
                print(f"✅ 程式碼在第 {attempt + 1} 次嘗試時編譯成功！")
                generation_result["arduino_code"] = code
                return True, generation_result
            
            if attempt == 2: break

            print(f"❌ 第 {attempt + 1} 次編譯失敗，將錯誤資訊傳送給 AI 進行修復...")
            error_message = result.get("stderr", "未知的編譯錯誤")
            fix_prompt = f"""你是 Arduino 程式碼專家。你之前生成的程式碼在編譯時出現了以下錯誤。請仔細分析錯誤訊息，並修正程式碼。

### 原始需求與接線:
**用戶需求**: {self.user_description}
**你建議的接線**: {generation_result.get("wiring_instructions")}

### 有問題的程式碼:
```arduino
{code}
```

### 編譯錯誤訊息:
```
{error_message}
```

### 修正指示:
1.  **分析錯誤**: 找出錯誤的根本原因。
2.  **提供完整修正版**: 僅修正 `"arduino_code"` 的內容。
3.  **保持 JSON 格式**: 你的回應必須是包含 `"arduino_code"` 和 `"wiring_instructions"` 的完整 JSON 物件。`"wiring_instructions"` 應保持不變。

請開始修正 JSON 物件："""
            
            fixed_response_text = await self._call_gemini_api(fix_prompt, is_json_output=True)
            if not fixed_response_text:
                print("❌ AI 未能提供修復後的程式碼。")
                return False, generation_result
            
            fixed_result = self._extract_code_from_json(fixed_response_text)
            if fixed_result and "arduino_code" in fixed_result:
                code = fixed_result["arduino_code"]
                generation_result = fixed_result
            else:
                print("❌ AI 的修復回應格式不正確。")
                return False, generation_result

        print("❌ AI 經過多次修復後仍然編譯失敗。")
        return False, generation_result

    async def _compile_code(self, code: str, fqbn: str) -> Tuple[bool, dict]:
        """內部使用的編譯函式，返回成功狀態和結果。"""
        with tempfile.TemporaryDirectory(prefix="arduino_compile_") as temp_dir:
            sketch_dir = os.path.join(temp_dir, "temp_sketch")
            os.makedirs(sketch_dir)
            with open(os.path.join(sketch_dir, "temp_sketch.ino"), 'w', encoding='utf-8') as f:
                f.write(code)

            compile_cmd = [self.arduino_cli_path, "compile", "--fqbn", fqbn, sketch_dir]
            proc = await asyncio.create_subprocess_exec(*compile_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()
            
            return proc.returncode == 0, {"stdout": stdout.decode(errors='ignore'), "stderr": stderr.decode(errors='ignore')}
            
    async def deploy_to_arduino(self, code: str, port: str = "auto", fqbn: str = "Seeeduino:samd:seeed_XIAO_m0") -> Dict[str, Any]:
        """部署程式到指定的 Arduino 埠，採用穩健的兩步驟流程。"""
        if not self.arduino_cli_path:
            return {"success": False, "error": "Arduino CLI 未安裝，無法部署"}
        
        await self.setup_cli_environment(code, fqbn)

        if port == "auto":
            devices = self.detect_arduino_devices()
            arduino_ports = [d['port'] for d in devices if d['is_arduino']]
            if not arduino_ports:
                return {"success": False, "error": "未找到任何 Arduino 設備", "suggestion": "請檢查 USB 連接或驅動程式。"}
            port = arduino_ports[0]
            print(f"🎯 自動選擇部署埠: {port}")

        with tempfile.TemporaryDirectory(prefix="arduino_deploy_") as temp_dir:
            sketch_dir = os.path.join(temp_dir, "deploy_sketch")
            os.makedirs(sketch_dir)
            with open(os.path.join(sketch_dir, "deploy_sketch.ino"), 'w', encoding='utf-8') as f:
                f.write(code)

            print(f"🔧 步驟 1/2: 正在編譯草稿碼...")
            compile_cmd = [self.arduino_cli_path, "compile", "--fqbn", fqbn, sketch_dir]
            compile_proc = await asyncio.create_subprocess_exec(*compile_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, compile_stderr_bytes = await compile_proc.communicate()

            if compile_proc.returncode != 0:
                return {"success": False, "error": "部署前編譯失敗", "details": compile_stderr_bytes.decode(errors='ignore')}
            
            print("✅ 編譯成功！")

            print(f"📤 步驟 2/2: 正在上傳至 {port}...")
            upload_cmd = [self.arduino_cli_path, "upload", "-p", port, "--fqbn", fqbn, sketch_dir, "--verbose"]
            upload_proc = await asyncio.create_subprocess_exec(*upload_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            upload_stdout_bytes, upload_stderr_bytes = await upload_proc.communicate()
            
            if upload_proc.returncode != 0:
                return {"success": False, "error": "上傳失敗", "details": upload_stderr_bytes.decode(errors='ignore')}
            
            return {"success": True, "message": "程式碼已成功部署！", "port": port, "output": upload_stdout_bytes.decode(errors='ignore')}


# --------------------------------------------------------------------------
# Global Instance and HTML Template
# --------------------------------------------------------------------------
arduino_system = TextDrivenArduinoSystem()

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>文字驅動Arduino自動生成系統</title>
    <!-- 引入 Marked.js 用於渲染 Markdown -->
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; margin: 0; background-color: #f0f2f5; color: #1c1e21; }
        .container { max-width: 900px; margin: 2rem auto; padding: 0 1rem; }
        h1 { color: #1877f2; text-align: center; font-size: 2.5rem; }
        .card { background-color: #fff; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); padding: 1.5rem; margin-top: 1.5rem; }
        .card h3 { margin-top: 0; color: #1877f2; border-bottom: 1px solid #dddfe2; padding-bottom: 0.75rem; font-size: 1.25rem; }
        textarea { width: 100%; box-sizing: border-box; min-height: 150px; padding: 0.75rem; border-radius: 6px; border: 1px solid #dddfe2; font-size: 1rem; resize: vertical; margin-bottom: 1rem;}
        #codeEditor {
            background-color: #282c34; color: #abb2bf; padding: 1rem; border-radius: 6px; 
            font-family: 'Menlo', 'Monaco', 'Consolas', monospace; font-size: 0.95rem; line-height: 1.5;
            border: 1px solid #444; min-height: 300px; resize: vertical;
        }
        /* 【新】接線指南的樣式 */
        #wiringGuide { border-left: 4px solid #4285f4; padding-left: 1rem; margin-bottom: 1.5rem; background-color: #f8f9fa; padding: 1rem; border-radius: 6px;}
        #wiringGuide h3, #wiringGuide h4 { margin-top: 0; }
        #wiringGuide ul, #wiringGuide ol { padding-left: 20px; }
        .btn { background-color: #1877f2; color: white; border: none; padding: 0.75rem 1.5rem; border-radius: 6px; cursor: pointer; font-size: 1rem; font-weight: bold; transition: background-color 0.3s; text-align: center; }
        .btn:hover:not(:disabled) { background-color: #166fe5; }
        .btn:disabled { background-color: #a0bdf1; cursor: not-allowed; }
        .btn-secondary { background-color: #e4e6eb; color: #4b4f56; }
        .btn-secondary:hover:not(:disabled) { background-color: #d8dade; }
        .btn-group { display: flex; flex-wrap: wrap; gap: 1rem; margin-top: 1rem; }
        .status { padding: 0.75rem; margin-top: 1rem; border-radius: 6px; border: 1px solid transparent; }
        .status.success { background-color: #e9f6ec; color: #34a853; border-color: #34a853; }
        .status.error { background-color: #fdeded; color: #ea4335; border-color: #ea4335; }
        .status.info { background-color: #e8f0fe; color: #4285f4; border-color: #4285f4; }
        .status-box { max-height: 200px; overflow-y: auto; font-size: 0.9rem; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 文字驅動Arduino自動生成系統</h1>
        <div class="card">
            <h3>1. 描述你的專案需求</h3>
            <textarea id="projectDescription" placeholder="範例：我有一個 Seeeduino XIAO，上面接了一個 8 顆燈的 NeoPixel 燈條。然後有一個sg90馬達。我想要加入一個按鈕，按下後馬達來回旋轉，同時燈條也對應變化。"></textarea>
            <div class="btn-group">
                <button class="btn" id="generateBtn" onclick="generateDirectCode()">🚀 AI 生成程式碼與接線指南</button>
            </div>
        </div>
        <div class="card">
            <h3>2. 系統與部署</h3>
            <div class="btn-group">
                 <button class="btn btn-secondary" onclick="checkSystemStatus()">🔄 檢查設備狀態</button>
                 <button class="btn" id="deployBtn" onclick="deployCode()" disabled>🛰️ 部署到 Arduino</button>
            </div>
            <div id="systemStatus" class="status-box" style="margin-top:1rem;">點擊按鈕以檢查...</div>
        </div>
        <div class="card" id="resultCard" style="display:none;">
            <!-- 【新】接線指南顯示區 -->
            <div id="wiringGuide" style="display:none;"></div>
            <h3>3. 結果與程式碼 (可編輯)</h3>
            <div id="resultMessage"></div>
            <textarea id="codeEditor" style="display:none; margin-top: 1rem;"></textarea>
        </div>
    </div>
    <script>
        const generateBtn = document.getElementById('generateBtn');
        const deployBtn = document.getElementById('deployBtn');
        const systemStatusDiv = document.getElementById('systemStatus');
        const resultCard = document.getElementById('resultCard');
        const resultMessageDiv = document.getElementById('resultMessage');
        const codeEditor = document.getElementById('codeEditor');
        const wiringGuideDiv = document.getElementById('wiringGuide');

        function showStatus(element, message, type = 'info') {
            element.innerHTML = `<div class="status ${type}">${message}</div>`;
        }

        async function checkSystemStatus() {
            showStatus(systemStatusDiv, '檢查中...', 'info');
            try {
                const response = await fetch('/api/debug/devices');
                const data = await response.json();
                let html = '<h4>Arduino CLI</h4>';
                if (data.arduino_cli && data.arduino_cli.success) {
                    html += `<p class="status success">✅ 可用: ${data.arduino_cli.version}</p>`;
                } else {
                    html += `<p class="status error">❌ 不可用: ${(data.arduino_cli ? data.arduino_cli.error : '未找到')}</p>`;
                }
                html += '<h4>檢測到的設備</h4>';
                if (data.devices.length > 0) {
                     data.devices.forEach(d => {
                        html += `<p class="status ${d.is_arduino ? 'success' : ''}">${d.is_arduino ? '✅' : 'ℹ️'} ${d.port}: ${d.description || 'N/A'}</p>`;
                    });
                } else {
                    html += '<p>未找到任何串列埠設備。</p>';
                }
                systemStatusDiv.innerHTML = html;
            } catch (e) {
                showStatus(systemStatusDiv, `檢查失敗: ${e.message}`, 'error');
            }
        }

        async function generateDirectCode() {
            const description = document.getElementById('projectDescription').value;
            if (!description) { alert('請輸入專案描述！'); return; }

            generateBtn.disabled = true;
            generateBtn.textContent = '🧠 AI 生成中...';
            deployBtn.disabled = true;
            resultCard.style.display = 'block';
            codeEditor.style.display = 'none';
            wiringGuideDiv.style.display = 'none';
            showStatus(resultMessageDiv, '🤖 正在呼叫 AI 生成程式碼與指南，這可能需要一些時間...', 'info');

            try {
                const response = await fetch('/api/generate-direct-code', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ description })
                });
                const result = await response.json();
                
                if (!response.ok) {
                    let errorMsg = result.error || '未知錯誤';
                    if (result.arduino_code) { // 即使失敗也可能返回程式碼
                        codeEditor.value = "// ---- 以下為最終失敗的程式碼 ----\\n\\n" + result.arduino_code;
                        codeEditor.style.display = 'block';
                    }
                    throw new Error(errorMsg);
                }
                
                showStatus(resultMessageDiv, `✅ ${result.message}`, 'success');
                
                // 【新】顯示接線指南
                if (result.wiring_instructions) {
                    wiringGuideDiv.innerHTML = marked.parse(result.wiring_instructions);
                    wiringGuideDiv.style.display = 'block';
                }

                codeEditor.value = result.arduino_code;
                codeEditor.style.display = 'block';
                deployBtn.disabled = false;

            } catch (e) {
                showStatus(resultMessageDiv, `❌ 處理失敗: ${e.message}`, 'error');
            } finally {
                generateBtn.disabled = false;
                generateBtn.textContent = '🚀 AI 生成程式碼與接線指南';
            }
        }
        
        async function deployCode() {
            const codeToDeploy = codeEditor.value;
            if (!codeToDeploy) {
                alert('編輯區沒有程式碼可以部署！');
                return;
            }

            deployBtn.disabled = true;
            deployBtn.textContent = '🛰️ 部署中...';
            showStatus(systemStatusDiv, '🚀 正在編譯和上傳您編輯後的程式碼，請稍候...', 'info');

            try {
                const response = await fetch('/api/deploy', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ port: 'auto', code: codeToDeploy })
                });
                const result = await response.json();
                if (!result.success) {
                    let errorMsg = `<h4>❌ 部署失敗: ${result.error}</h4>`;
                    if(result.details) errorMsg += `<pre>${result.details}</pre>`;
                    if(result.suggestion) errorMsg += `<p><strong>建議:</strong> ${result.suggestion}</p>`;
                    throw new Error(errorMsg);
                }
                showStatus(systemStatusDiv, `✅ 成功部署到 ${result.port}！`, 'success');
            } catch (e) {
                showStatus(systemStatusDiv, e.message, 'error');
            } finally {
                deployBtn.disabled = false;
                deployBtn.textContent = '🛰️ 部署到 Arduino';
            }
        }

        window.onload = checkSystemStatus;
    </script>
</body>
</html>
"""

# --------------------------------------------------------------------------
# FastAPI API Routes
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_root():
    """提供主頁面 HTML。"""
    return HTMLResponse(content=HTML_TEMPLATE)

@app.post("/api/generate-direct-code")
async def api_generate_direct_code(request: Dict[str, str]):
    """API 端點：接收用戶描述，調用核心功能生成並驗證程式碼。"""
    description = request.get('description', '')
    if not description:
        return JSONResponse(status_code=400, content={"error": "請提供專案描述"})
    
    arduino_system.user_description = description

    try:
        generation_result = await arduino_system.generate_code_and_instructions(description)
        if not generation_result or "arduino_code" not in generation_result:
            return JSONResponse(status_code=500, content={"error": "AI程式碼生成失敗，模型未返回有效內容或格式錯誤。"})
        
        is_valid, final_result = await arduino_system.validate_and_fix_code(generation_result)
        
        if not is_valid:
            return JSONResponse(status_code=400, content={
                "error": "生成的程式碼無法通過驗證與修復，請檢查需求或稍後再試。", 
                "arduino_code": final_result.get("arduino_code"),
                "wiring_instructions": final_result.get("wiring_instructions")
            })
        
        return JSONResponse(content={
            "arduino_code": final_result.get("arduino_code"), 
            "wiring_instructions": final_result.get("wiring_instructions"),
            "message": "AI成功生成程式碼與接線指南",
        })
    except ValueError as e:
        return JSONResponse(status_code=400, content={"error": str(e)})
    except Exception as e:
        print(f"伺服器內部錯誤: {e}")
        return JSONResponse(status_code=500, content={"error": "伺服器處理時發生未預期的錯誤。"})

@app.post("/api/deploy")
async def api_deploy_code(request: Dict[str, str]):
    """【新】API 端點：從前端請求中獲取程式碼並進行部署。"""
    code_to_deploy = request.get("code")
    if not code_to_deploy:
        return JSONResponse(status_code=400, content={"error": "請求中未包含程式碼"})
    
    port = request.get("port", "auto")
    result = await arduino_system.deploy_to_arduino(code_to_deploy, port=port)
    return JSONResponse(content=result)

@app.get("/api/debug/devices")
async def debug_devices():
    """API 端點：調試用，列出所有檢測到的設備和 CLI 狀態。"""
    arduino_system.arduino_cli_path = arduino_system.find_arduino_cli()
    cli_version = "N/A"
    cli_success = False
    if arduino_system.arduino_cli_path:
        try:
            res = subprocess.run([arduino_system.arduino_cli_path, "version"], capture_output=True, text=True, check=True)
            cli_version = res.stdout.strip()
            cli_success = True
        except Exception:
             cli_version = "無法執行"
    
    devices = arduino_system.detect_arduino_devices()
    return JSONResponse(content={"devices": devices, "arduino_cli": {"success": cli_success, "version": cli_version}, "arduino_count": len([d for d in devices if d['is_arduino']])})

# --------------------------------------------------------------------------
# Main Execution Block
# --------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    print("🚀 啟動文字驅動Arduino自動生成系統...")
    print("🧠 使用 Gemini AI 進行智能分析和程式生成")
    print("🌐 請在瀏覽器中開啟: http://127.0.0.1:8000")
    if not arduino_system.google_api_key:
        print("⚠️  警告: 環境變數 GOOGLE_API_KEY 未設定，AI 功能將無法使用。")
    uvicorn.run(app, host="0.0.0.0", port=8000)
