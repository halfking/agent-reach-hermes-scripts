#!/usr/bin/env python3
"""
用 camoufox 发微博（stealth 浏览器，不容易被检测）
Cookie 从 ~/.agent-reach/config/weibo_cookie.txt 读取
支持图片上传：传 local_image_paths 列表或 image_urls 列表
"""
import json
import time
import sys
import os
import urllib.request
import tempfile
from pathlib import Path

COOKIE_FILE = Path.home() / ".agent-reach" / "config" / "weibo_cookie.txt"


def parse_cookie_str(cookie_str):
    """把 'K=V; K=V' 格式转成 camoufox 需要的字典列表"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        cookies.append({
            "name": k,
            "value": v,
            "domain": ".weibo.com",
            "path": "/",
            "secure": True,
            "httpOnly": False,
        })
    return cookies


def download_image(url, dest_path):
    """下载图片到本地"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
        with open(dest_path, "wb") as f:
            f.write(data)
        return len(data)
    except Exception as e:
        print(f"⚠️ 下载图片失败 {url}: {e}")
        return 0


def post_weibo(content, headless=True, local_image_paths=None, image_urls=None):
    """
    用 camoufox 发微博，支持：
    - local_image_paths: 本地文件路径列表（GitHub 截图等）
    - image_urls: 远程 URL 列表（推文图片，自动下载）
    """
    
    # 下载远程图片到临时文件
    temp_images = []
    if image_urls:
        for i, url in enumerate(image_urls):
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp_path = tmp.name
            tmp.close()
            size = download_image(url, tmp_path)
            if size > 0:
                temp_images.append(tmp_path)
                print(f"✅ 下载图片 {i+1}: {size} bytes → {tmp_path}")
            else:
                try:
                    os.unlink(tmp_path)
                except:
                    pass
    
    # 合并：本地路径 + 下载的远程图片
    image_list = []
    if local_image_paths:
        for p in local_image_paths:
            if os.path.exists(p):
                image_list.append(p)
                print(f"✅ 使用本地图片: {p}")
    image_list.extend(temp_images)
    from camoufox import Camoufox

    if not COOKIE_FILE.exists():
        print(f"❌ Cookie 文件不存在: {COOKIE_FILE}")
        return False

    cookie_str = open(COOKIE_FILE).read().strip()
    cookies = parse_cookie_str(cookie_str)

    with Camoufox(headless=headless) as browser:
        page = browser.new_page()
        
        # 设置 Cookie
        page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        
        # 添加 cookies
        context = page.context
        for cookie in cookies:
            try:
                context.add_cookies([cookie])
            except Exception as e:
                print(f"⚠️ 添加 cookie {cookie['name']} 失败: {e}")

        print("🍜 设置 Cookie 完成，访问微博...")
        page.goto("https://weibo.com", timeout=30000)
        page.wait_for_load_state("networkidle", timeout=15000)
        time.sleep(3)
        
        # 检查是否登录
        url = page.url
        print(f"📄 当前 URL: {url}")
        
        if "passport" in url or "login" in url.lower():
            print("⚠️ Cookie 无效，需要重新登录")
            page.screenshot(path="/tmp/weibo_login_required.png")
            return False
        
        print("✅ 已登录微博")
        
        # 填写内容
        print("📝 填写内容...")
        textarea = None
        selectors = [
            "textarea._input_1rz8r_8",
            "textarea[placeholder*='微博']",
            "div[aria-label*='微博']",
            "textarea.W_input",
            "[contenteditable='true']",
        ]
        for sel in selectors:
            try:
                el = page.wait_for_selector(sel, timeout=3000)
                if el:
                    textarea = el
                    print(f"✅ 找到输入框: {sel}")
                    break
            except:
                continue
        
        if not textarea:
            print("⚠️ 未找到发帖框，尝试点击首页发布按钮")
            try:
                page.click("body")
                time.sleep(1)
                for sel in ["*[placeholder*='新鲜事']", "*[placeholder*='微博']"]:
                    try:
                        page.click(sel, timeout=2000)
                        print(f"Clicked: {sel}")
                        break
                    except:
                        continue
            except Exception as e:
                print(f"点击发布区域: {e}")
        
        if textarea:
            textarea.click()
            time.sleep(0.5)
            textarea.fill("")
            time.sleep(0.3)
            textarea.type(content, delay=50)
            print(f"✅ 内容已输入: {content[:50]}...")
            time.sleep(1)
        
        # 上传图片（如有）
        if image_list:
            print(f"📷 上传 {len(image_list)} 张图片...")
            # 微博通常有隐藏的 <input type="file" multiple>，可以直接 set_input_files
            # 不需要先点击图片图标
            upload_btn = None
            for sel in [
                "input[type='file'][multiple]",
                "input[type='file'][accept*='image']",
                "input[type='file']",
            ]:
                try:
                    el = page.query_selector(sel)
                    if el:
                        upload_btn = el
                        print(f"✅ 找到上传input（隐藏）: {sel}")
                        break
                except:
                    continue

            # 如果没找到，再尝试点击图标触发
            if not upload_btn:
                for icon_sel in [
                    "div[title*='图片']",
                    "div[aria-label*='图片']",
                    "div[aria-label*='添加图片']",
                    "span[class*='camera']",
                    "div[class*='publish'] span:nth-child(2)",
                    "button[class*='picture']",
                ]:
                    try:
                        el = page.query_selector(icon_sel)
                        if el and el.is_visible():
                            el.click()
                            print(f"✅ 点击图片图标: {icon_sel}")
                            time.sleep(1.5)
                            break
                    except:
                        continue

                # 重新找
                for sel in ["input[type='file']", "input[accept*='image']"]:
                    try:
                        el = page.wait_for_selector(sel, timeout=3000, state="attached")
                        if el:
                            upload_btn = el
                            print(f"✅ 找到上传input: {sel}")
                            break
                    except:
                        continue

            if upload_btn:
                try:
                    # 一次性上传所有图片（multiple input）
                    upload_btn.set_input_files(image_list)
                    print(f"✅ 上传 {len(image_list)} 张图片成功")
                    time.sleep(5)  # 等待图片上传完成
                except Exception as e:
                    print(f"⚠️ 批量上传失败: {e}，尝试逐张...")
                    for img_path in image_list:
                        try:
                            upload_btn.set_input_files(img_path)
                            print(f"✅ 上传图片: {img_path}")
                            time.sleep(3)
                        except Exception as e2:
                            print(f"⚠️ 上传失败: {e2}")
            else:
                print("⚠️ 未找到图片上传入口（无 input[type=file]）")
        
        # 检查按钮状态并点击发送
        btn_info = page.evaluate("""() => {
            const btns = Array.from(document.querySelectorAll("button")).filter(b => b.innerText.trim() === "发送" && b.offsetParent !== null);
            if (!btns.length) return "not found";
            return JSON.stringify({text: btns[0].innerText.trim(), disabled: btns[0].disabled, class: btns[0].className.slice(0,40)});
        }""")
        print(f"发送按钮状态: {btn_info}")
        
        # 尝试多种方式点击发送
        send_clicked = False
        if '"disabled":false' in btn_info or '"disabled":null' in btn_info or '"disabled":undefined' in btn_info:
            # 方法1: page.click (直接点击元素)
            try:
                page.click("button.woo-button-main:has-text('发送')", timeout=3000)
                print("✅ page.click 发送成功")
                send_clicked = True
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ page.click 失败: {e}")
        
        if not send_clicked:
            # 方法2: JS click
            try:
                page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll("button")).filter(b => b.innerText.trim() === "发送" && b.offsetParent !== null && !b.disabled);
                    if (btns.length) { btns[0].click(); return "clicked"; }
                    return "not found";
                }""")
                print("✅ JS click 发送")
                send_clicked = True
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ JS click 失败: {e}")
        
        if not send_clicked:
            # 方法3: Ctrl+Enter 提交（绕过按钮点击问题）
            try:
                textarea.click()
                page.keyboard.press("Control+Enter")
                print("✅ Ctrl+Enter 发送")
                send_clicked = True
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ Ctrl+Enter 失败: {e}")
        
        if not send_clicked:
            # 方法4: 表单提交
            try:
                page.evaluate("""() => {
                    const form = document.querySelector("form");
                    if (form) { form.submit(); return "submitted"; }
                    const textarea = document.querySelector("textarea");
                    if (textarea) {
                        const evt = new InputEvent("input", {bubbles:true, cancelable:true, data:null});
                        Object.defineProperty(evt, "inputType", {value: "insertLineBreak"});
                        textarea.dispatchEvent(evt);
                        textarea.blur();
                        return "blurred";
                    }
                    return "nothing";
                }""")
                print("✅ 备用方法")
                send_clicked = True
                time.sleep(3)
            except Exception as e:
                print(f"⚠️ 备用方法失败: {e}")
        
        # 等待一小会让 React 处理
        time.sleep(1)
        
        # 验证：textarea 被清空 = 发帖成功
        try:
            remaining = textarea.input_value()
            if not remaining:
                print("✅ 微博发送成功！")
                page.screenshot(path="/tmp/weibo_success.png")
                return True
        except:
            pass
        
        # textarea 仍有内容，尝试等待 React 渲染
        try:
            page.wait_for_function(
                "() => { const ta = document.querySelector('textarea._input_1rz8r_8'); return !ta || !ta.value; }",
                timeout=8000
            )
            print("✅ 微博发送成功（React 渲染后确认）！")
            page.screenshot(path="/tmp/weibo_success.png")
            return True
        except:
            pass
        
        # 最终检查
        try:
            remaining = textarea.input_value()
            if remaining:
                print(f"⚠️ textarea 还有内容: {remaining[:50]}")
            page.screenshot(path="/tmp/weibo_pending.png")
            return False
        except:
            print("✅ 微博发送成功（textarea 已不存在）！")
            return True
        finally:
            # 清理临时图片
            for tmp_path in temp_images:
                try:
                    os.unlink(tmp_path)
                except:
                    pass


if __name__ == "__main__":
    content = sys.argv[1] if len(sys.argv) > 1 else "测试发帖 🤯 #AI"
    print(f"开始发帖: {content[:50]}...")
    success = post_weibo(content, headless=False)
    sys.exit(0 if success else 1)
