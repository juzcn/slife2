---
name: browser-harness-headless
description: "Windows 下用 browser-harness 连接无头 Chrome（CDP 9222 端口）执行浏览器自动化：启动独立无头实例、通过 BU_CDP_URL 连接、用 cmd 管道传 Python 脚本。适用于后台网页任务、爬取、截图，不影响用户日常浏览器。"
---

# browser-harness 无头模式（Windows）

在 Windows 上以**无头方式**运行 browser-harness，不打扰用户现有的浏览器窗口。

## 适用场景

- 后台网页自动化 / 爬取 / 截图
- 批量任务（多个无头实例并行）
- 服务器或 CI 环境（无显示器）

## 一、启动独立无头 Chrome

```powershell
Start-Process 'C:\Program Files\Google\Chrome\Application\chrome.exe' `
  -ArgumentList '--headless=new', '--remote-debugging-port=9222', `
  '--user-data-dir=C:\Dev\Workspace\slife\.bh-test-profile', `
  '--disable-gpu', 'about:blank'
```

> ⚠️ **必须使用独立 `--user-data-dir`**，否则会复用已运行的 Chrome 实例而忽略调试端口参数。

## 二、验证 CDP 端口

```python
import urllib.request
r = urllib.request.urlopen('http://127.0.0.1:9222/json/version', timeout=5)
print(r.status)  # 200 = 成功
```

若端口未开，检查进程：

```powershell
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" | Where-Object { $_.CommandLine -like '*9222*' }
netstat -ano | Select-String '9222'
```

## 三、调用 browser-harness（Windows 关键点）

### ❌ 不要用 heredoc（那是 bash 语法）

```bash
# 这在 PowerShell/cmd 下会失败！
browser-harness <<'PY' ... PY
```

### ✅ 正确方式：写脚本文件 + cmd 管道

1. 写一个 `.py` 脚本（helper 已预导入）：

```python
# bh_task.py
new_tab("https://example.com")
print(page_info())
```

2. 通过 cmd 管道执行：

```cmd
set BU_CDP_URL=http://127.0.0.1:9222&& type bh_task.py | browser-harness
```

## 四、常用 helper

| Helper | 作用 |
|--------|------|
| `new_tab(url)` | 新标签页打开 URL |
| `page_info()` | 当前页 URL/标题/尺寸 |
| `js("document.body.innerText")` | 执行 JS 取 DOM 内容 |
| `capture_screenshot(path)` | 截图保存到路径 |
| `wait_for_load()` | 等待页面加载完成 |
| `ensure_real_tab()` | 切换到真实标签页 |
| `cdp("Domain.method", ...)` | 直接调用 CDP 协议 |
| `click_at_xy(x, y)` | 坐标点击 |

## 五、诊断 & 更新

```bash
browser-harness --doctor          # 诊断安装/daemon/浏览器状态
browser-harness --update -y       # 升级到最新版
browser-harness --reload          # 重启 daemon 使代码变更生效
browser-harness skill             # 查看完整 skill 文档
```

## 六、常见坑

1. **heredoc 报错**（`�ʱ��Ӧ�� <<`）：PowerShell/cmd 不支持 `<<'PY'`，用 cmd 管道 + 文件。
2. **端口没开但进程在**：等 3-5 秒再试；确认用独立 user-data-dir 启动。
3. **复用了现有浏览器**：Chrome 已有实例时新参数被忽略，必须用独立 `--user-data-dir`。
4. **引号转义地狱**：不要在 PowerShell 里写内联 `python -c "..."` 改文件/传参，一律写脚本文件再执行。
5. **云浏览器**（Browser Use Cloud）需要 `browser-harness auth login` + API key；本地无头不需要。

## 七、完整示例（爬取页面正文）

```python
# scrape.py
new_tab("https://example.com")
wait_for_load()
html = js("document.body.innerText")
print(html[:500])
capture_screenshot("C:/Dev/Workspace/slife/shot.png")
```

```cmd
set BU_CDP_URL=http://127.0.0.1:9222&& type scrape.py | browser-harness
```
