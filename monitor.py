import cfscrape
from bs4 import BeautifulSoup
import asyncio
import json
import os
import re
import fcntl
import sys
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest

def acquire_lock(lock_file_path='/tmp/monitor_script.lock'):
    """使用文件锁防止脚本重复运行。"""
    lock_file = open(lock_file_path, 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file  # 返回锁文件对象以便在程序结束时释放锁
    except IOError:
        print("Another instance of the script is already running. Exiting.")
        sys.exit(1)

def escape_markdown(text):
    """不进行任何转义操作，直接返回文本。"""
    return text

async def fetch_html(url, retries=3):
    scraper = cfscrape.create_scraper()  # 使用cfscrape创建scraper对象
    for attempt in range(retries):
        try:
            response = scraper.get(url)
            if response.status_code == 200:
                return response.text
            else:
                print(f"Warning: Received status code {response.status_code} for URL {url}")
        except Exception as e:
            print(f"Error fetching {url}: {e} (Attempt {attempt + 1} of {retries})")
            await asyncio.sleep(2)  # 重试等待时间
    return None  # 如果所有尝试都失败，返回 None

def parse_stock(html, out_of_stock_text):
    try:
        soup = BeautifulSoup(html, 'html.parser')
        stock_match = re.search(r'(\d+)\s+in stock', soup.get_text(), re.IGNORECASE)
        if stock_match:
            return int(stock_match.group(1))
        elif out_of_stock_text in soup.get_text():
            return 0
        else:
            return float('inf')
    except Exception as e:
        print(f"Error parsing HTML: {e}")
        return None  # 如果解析失败，返回 None

async def check_stock(url, out_of_stock_text):
    html = await fetch_html(url)
    if html is None:
        return None  # 如果获取失败，返回 None
    return parse_stock(html, out_of_stock_text)

async def load_config(filename='/root/monitor/商家名字/config.json'):
    current_dir = os.getcwd()
    config_path = os.path.join(current_dir, filename)
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)

async def send_notification(config, merchant, stock, stock_quantity, message_id=None):
    bot = Bot(token=config['telegram_token'])
    try:
        title = f"{merchant['name']}-{stock['title']}"
        tag = escape_markdown(merchant['tag'])
        price = escape_markdown(stock['price'])
        hardware_info = f"[{escape_markdown(stock['hardware_info'])}]({stock['url']})"
        
        # 动态生成库存状态信息，无论有货还是无货都嵌入链接
        stock_info = f"🛒[库存：{'有' if stock_quantity > 0 else '无'}]({stock['url']})"

        # 生成优惠码信息
        monthly_coupon = f"Monthly：`{merchant['coupon_monthly']}`" if merchant.get('coupon_monthly') else ""
        annual_coupon = f"Annually：`{merchant['coupon_annual']}`" if merchant.get('coupon_annual') else ""
        coupon_info = "\n".join(filter(None, [monthly_coupon, annual_coupon]))
        coupon_section = f"\n\n{coupon_info}\n\n" if coupon_info else "\n\n"

        # 构建最终消息
        message = (
            f"{title}\n\n{tag}\n\nℹ️{hardware_info}{coupon_section}💰Price: {price}\n\n{stock_info}"
        )

        if stock_quantity > 0:
            # 发送新消息
            sent_message = await bot.send_message(
                chat_id=config['telegram_chat_id'], text=message, parse_mode=ParseMode.MARKDOWN
            )
            print("通知发送成功")
            return sent_message.message_id  # 返回消息ID以供后续修改
        else:
            # 修改现有的消息内容为缺货状态
            if message_id:
                try:
                    await bot.edit_message_text(
                        chat_id=config['telegram_chat_id'], message_id=message_id, text=message, parse_mode=ParseMode.MARKDOWN
                    )
                    print("消息已更新为缺货状态")
                except BadRequest as e:
                    print(f"Error editing message: {e}")
    except Exception as e:
        print(f"Error sending notification: {e}")
    return None

async def main():
    lock_file = acquire_lock()  # 确保只运行一个实例

    try:
        merchant_status = {}
        message_ids = {}  # 用于存储每个商品的消息ID

        while True:
            config = await load_config()  # 每次循环时重新加载配置文件
            check_interval = config.get('check_interval', 600)  # 更新检查间隔

            for merchant in config['merchants']:
                print(f"开始检查商家: {merchant['name']}")
                out_of_stock_text = merchant.get('out_of_stock_text', '缺货')
                for stock in merchant['stock_urls']:
                    url = stock['url']
                    previous_status = merchant_status.get(merchant['name'], {}).get(url, {'in_stock': False})
                    stock_quantity = await check_stock(url, out_of_stock_text)
                    if stock_quantity is None:
                        print(f"Skipping URL {url} due to repeated errors.")
                        continue

                    # 当库存有货且之前状态是缺货时，发送新通知
                    if stock_quantity > 0 and not previous_status['in_stock']:
                        message_id = await send_notification(config, merchant, stock, stock_quantity)
                        merchant_status.setdefault(merchant['name'], {})[url] = {'in_stock': True}
                        message_ids[url] = message_id  # 保存消息ID
                    # 当库存变为缺货且之前状态是有货时，更新消息而不是发送新通知
                    elif stock_quantity == 0 and previous_status['in_stock']:
                        await send_notification(config, merchant, stock, stock_quantity, message_ids.get(url))
                        merchant_status.setdefault(merchant['name'], {})[url] = {'in_stock': False}

            await asyncio.sleep(check_interval)
    finally:
        # 在程序退出时释放文件锁
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()

if __name__ == '__main__':
    asyncio.run(main())
