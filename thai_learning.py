# === 第一部分：初始化和基礎設定 ===
import os
import uuid
import random
import json
from datetime import datetime, timedelta
import io
import numpy as np
from pydub import AudioSegment
import requests
import logging
from dotenv import load_dotenv

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, AudioMessage, ImageMessage,
    TextSendMessage, ImageSendMessage, AudioSendMessage,
    TemplateSendMessage, ButtonsTemplate, MessageAction,
    URIAction, QuickReply, QuickReplyButton
)
import azure.cognitiveservices.speech as speechsdk
from google.cloud import storage

# 設置日誌
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 加載環境變數
load_dotenv()  # 載入 .env 文件中的環境變數 (本地開發用)

# === 應用初始化 ===
app = Flask(__name__)

# LINE Bot設定
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# Azure Speech Services設定
speech_key = os.environ.get('AZURE_SPEECH_KEY', 'YOUR_AZURE_SPEECH_KEY')
speech_region = os.environ.get('AZURE_SPEECH_REGION', 'eastasia')

# Google Cloud Storage 設定
GCS_BUCKET_NAME = os.environ.get('GCS_BUCKET_NAME', 'your-thai-learning-bucket')

logger.info(f"初始化應用程式... LINE Bot, Azure Speech 和 GCS 服務已配置")

# === Google Cloud Storage 輔助函數 ===

def init_gcs_client():
    """初始化 Google Cloud Storage 客戶端"""
    try:
        # 嘗試從環境變數獲取認證
        import json
        import tempfile
        
        # 1. 首先嘗試使用環境變數中的 JSON 內容
        creds_json = os.environ.get('GCS_CREDENTIALS')
        if creds_json:
            # 創建臨時文件存儲憑證
            with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as temp:
                temp.write(creds_json.encode('utf-8'))
                temp_file_name = temp.name
            
            # 使用臨時文件初始化客戶端
            storage_client = storage.Client.from_service_account_json(temp_file_name)
                       
            # 使用後刪除臨時文件
            os.unlink(temp_file_name)
            logger.info("使用環境變數 GCS_CREDENTIALS 成功初始化 Google Cloud Storage 客戶端")
            return storage_client
            
        # 2. 嘗試使用本地金鑰文件 (本地開發使用)
        local_keyfile_path = r"C:\Users\ids\Desktop\泰文學習的論文資料(除了)程式相關\泰文聊天機器人google storage 金鑰.json"
        if os.path.exists(local_keyfile_path):
            storage_client = storage.Client.from_service_account_json(local_keyfile_path)
            logger.info("使用本地金鑰文件成功初始化 Google Cloud Storage 客戶端")
            return storage_client
            
        # 3. 嘗試使用默認認證
        storage_client = storage.Client()
        logger.info("使用默認認證成功初始化 Google Cloud Storage 客戶端")
        return storage_client
    
    except Exception as e:
        logger.error(f"初始化 Google Cloud Storage 客戶端失敗: {str(e)}")
        return None

def upload_file_to_gcs(file_content, destination_blob_name, content_type=None):
    """上傳檔案到 Google Cloud Storage 並返回公開 URL"""
    try:
        # 初始化 GCS 客戶端
        storage_client = init_gcs_client()
        if not storage_client:
            logger.error("無法初始化 GCS 客戶端")
            return None
            
        # 獲取 bucket
        bucket = storage_client.bucket(GCS_BUCKET_NAME)
        
        # 創建一個新的 blob
        blob = bucket.blob(destination_blob_name)
        
        # 設置內容類型（如果提供）
        if content_type:
            blob.content_type = content_type
            
        # 上傳檔案
        if hasattr(file_content, 'read'):
            # 如果是檔案物件
            blob.upload_from_file(file_content, rewind=True)
        else:
            # 如果是二進制數據
            blob.upload_from_string(file_content)
            
        # 設置為公開可讀取
        blob.make_public()
        
        # 返回公開 URL
        logger.info(f"成功上傳檔案至 {destination_blob_name}, URL: {blob.public_url}")
        return blob.public_url
        
    except Exception as e:
        logger.error(f"上傳檔案到 GCS 時發生錯誤: {str(e)}")
        return None

# 測試 Azure 語音服務連接
def test_azure_connection():
    """測試 Azure 語音服務連接"""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        logger.info("Azure Speech Services 連接測試成功")
    except Exception as e:
        logger.error(f"Azure Speech Services 連接測試失敗: {str(e)}")

# 在模組層級調用這個函數
test_azure_connection()

# === LINE Bot Webhook 處理 ===
@app.route("/callback", methods=['POST'])
def callback():
    try:
        # 增加更詳細的錯誤處理
        signature = request.headers.get('X-Line-Signature', '')
        body = request.get_data(as_text=True)
        
        logger.info(f"收到回調，簽名: {signature}")
        logger.info(f"回調內容: {body}")
        
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        logger.error(f"簽名驗證失敗: {str(e)}")
        abort(400)
    except Exception as e:
        logger.error(f"處理回調時發生未知錯誤: {str(e)}")
        abort(500)
    
    return 'OK'
# === 第二部分：用戶數據管理和泰語學習資料 ===

# === 用戶數據管理 ===
class UserData:
    def __init__(self):
        self.users = {}
        # 添加臨時用戶數據存儲
        self.users['temp'] = {'game_state': {}}
        logger.info("初始化用戶數據管理器")
        # 在實際應用中，應該使用資料庫存儲這些數據
        logger.info("初始化用戶數據管理器")
        
    def get_user_data(self, user_id):
        """獲取用戶數據，如果不存在則初始化"""
        if user_id not in self.users:
            logger.info(f"為新用戶創建數據: {user_id}")
            self.users[user_id] = {
                'score': 0,
                'current_activity': None,
                'current_vocab': None,
                'current_category': None,
                'game_state': {},
                'vocab_mastery': {},
                'learning_progress': {},
                'last_active': self.current_date(),
                'streak': 0
            }
        return self.users[user_id]
    
    def current_date(self):
        """獲取當前日期，便於追蹤學習進度"""
        return datetime.now().strftime("%Y-%m-%d")
    
    def update_streak(self, user_id):
        """更新用戶的連續學習天數"""
        user_data = self.get_user_data(user_id)
        last_active = datetime.strptime(user_data['last_active'], "%Y-%m-%d")
        today = datetime.now()
        
        if (today - last_active).days == 1:  # 連續下一天學習
            user_data['streak'] += 1
            logger.info(f"用戶 {user_id} 連續學習天數增加到 {user_data['streak']} 天")
        elif (today - last_active).days > 1:  # 中斷了連續學習
            user_data['streak'] = 1
            logger.info(f"用戶 {user_id} 連續學習中斷，重置為 1 天")
        # 如果是同一天，streak保持不變
        
        user_data['last_active'] = self.current_date()

user_data_manager = UserData()

# === 泰語學習資料 ===
thai_data = {
    'categories': {
        'daily_phrases': {
            'name': '日常用語',
            'words': ['你好', '謝謝', '再見', '對不起', '早安', '晚安', '不客氣', '怎麼走？', '多少錢', '好吃']
        },
        'numbers': {
            'name': '數字',
            'words': ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十']
        },
        'animals': {
            'name': '動物',
            'words': ['貓', '狗', '鳥', '魚', '大象', '老虎', '猴子', '雞', '豬', '牛']
        },
        'food': {
            'name': '食物',
            'words': ['米飯', '麵', '啤酒', '麵包', '雞翅', '芒果糯米飯', '炒飯', '青木瓜沙拉', '冬蔭功湯', '泰式炒河粉']
        },
        'transportation': {
            'name': '交通工具',
            'words': ['車子', '公車', '計程車', '摩托車', '火車', '飛機', '船', '腳踏車', '嘟嘟車', '貨車']
        }
    },
    'basic_words': {
        # 日常用語
        '你好': {'thai': 'สวัสดี', 'pronunciation': 'sa-wat-dee', 'tone': 'mid-falling-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E4%BD%A0%E5%A5%BD.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/Hello.jpg'},
        '謝謝': {'thai': 'ขอบคุณ', 'pronunciation': 'khop-khun', 'tone': 'low-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E8%AC%9D%E8%AC%9D.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/thank.jpg'},
        '再見': {'thai': 'ลาก่อน', 'pronunciation': 'la-kon', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%86%8D%E8%A6%8B.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/bye.jpg'},
        '對不起': {'thai': 'ขอโทษ', 'pronunciation': 'kho-thot', 'tone': 'low-low',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%B0%8D%E4%B8%8D%E8%B5%B7.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/sorry.jpg'},
        '早安': {'thai': 'อรุณสวัสดิ์', 'pronunciation': 'a-run-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%97%A9%E5%AE%89.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/morning.jpg'},
        '晚安': {'thai': 'ราตรีสวัสดิ์', 'pronunciation': 'ra-tree-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%99%9A%E5%AE%89.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/night.jpg'},
        '不客氣': {'thai': 'ไม่เป็นไร', 'pronunciation': 'mai-pen-rai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E4%B8%8D%E5%AE%A2%E6%B0%A3.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/welcome.jpg'},
        '怎麼走？': {'thai': 'ไปทางไหน', 'pronunciation': 'pai-tang-nai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%80%8E%E9%BA%BC%E8%B5%B0.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/how%20can%20i%20go%20to.jpg'},
        '多少錢': {'thai': 'เท่าไหร่', 'pronunciation': 'tao-rai', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%A4%9A%E5%B0%91%E9%8C%A2.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/askprice.jpg'},
        '好吃': {'thai': 'อร่อย', 'pronunciation': 'a-roi', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%A5%BD%E5%90%83.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/yummy.jpg'},
        
        # 數字
        '一': {'thai': 'หนึ่ง', 'pronunciation': 'neung', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/1.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/1.png'},
        '二': {'thai': 'สอง', 'pronunciation': 'song', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/2.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/2.jpg'},
        '三': {'thai': 'สาม', 'pronunciation': 'sam', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/3.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/3.jpg'},
        '四': {'thai': 'สี่', 'pronunciation': 'see', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/4.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/4.jpg'},
        '五': {'thai': 'ห้า', 'pronunciation': 'ha', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/5.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/5.jpg'},
        '六': {'thai': 'หก', 'pronunciation': 'hok', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/6.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/6.jpg'},
        '七': {'thai': 'เจ็ด', 'pronunciation': 'jet', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/7.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/7.jpg'},
        '八': {'thai': 'แปด', 'pronunciation': 'paet', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/8.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/8.jpg'},
        '九': {'thai': 'เก้า', 'pronunciation': 'kao', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/9.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/9.jpg'},
        '十': {'thai': 'สิบ', 'pronunciation': 'sip', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/10.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/10.jpg'},
        
        # 動物
        '貓': {'thai': 'แมว', 'pronunciation': 'maew', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%B2%93.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%B2%93.jpg'},
        '狗': {'thai': 'หมา', 'pronunciation': 'ma', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%8B%97.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%8B%97.jpg'},
        '鳥': {'thai': 'นก', 'pronunciation': 'nok', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%B3%A5.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%B3%A5.jpg'},
        '魚': {'thai': 'ปลา', 'pronunciation': 'pla', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%AD%9A.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%AD%9A.jpg'},
        '大象': {'thai': 'ช้าง', 'pronunciation': 'chang', 'tone': 'high',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E5%A4%A7%E8%B1%A1.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E5%A4%A7%E8%B1%A1.jpg'},
        '老虎': {'thai': 'เสือ', 'pronunciation': 'suea', 'tone': 'low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%80%81%E8%99%8E.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%80%81%E8%99%8E.jpg'},
        '猴子': {'thai': 'ลิง', 'pronunciation': 'ling', 'tone': 'mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%8C%B4%E5%AD%90.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%8C%B4.jpg'},
        '雞': {'thai': 'ไก่', 'pronunciation': 'kai', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%9B%9E.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%9B%9E.jpg'},
        '豬': {'thai': 'หมู', 'pronunciation': 'moo', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%B1%AC.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%B1%AC.jpg'},
        '牛': {'thai': 'วัว', 'pronunciation': 'wua', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%89%9B.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%89%9B.jpg'},
        
        # 食物
        '米飯': {'thai': 'ข้าว', 'pronunciation': 'khao', 'tone': 'falling',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%B1%B3%E9%A3%AF.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/rice.jpg'},
        '粿條': {'thai': 'ก๋วยเตี๋ยว', 'pronunciation': 'guay-tiew', 'tone': 'falling-falling-low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%B2%BF%E6%A2%9D.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/%E7%B2%BF%E6%A2%9D.jpg'},
        '啤酒': {'thai': 'เบียร์', 'pronunciation': 'bia', 'tone': 'mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E5%95%A4%E9%85%92.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/beer.jpg'},
        '麵包': {'thai': 'ขนมปัง', 'pronunciation': 'kha-nom-pang', 'tone': 'mid-mid-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%BA%B5%E5%8C%85.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/bread.jpg'},
        '雞翅': {'thai': 'ปีกไก่', 'pronunciation': 'peek-kai', 'tone': 'falling-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%9B%9E%E7%BF%85.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/chicken%20wing.jpg'},
        '芒果糯米飯': {'thai': 'ข้าวเหนียวมะม่วง', 'pronunciation': 'khao-niew-ma-muang', 'tone': 'falling-falling-mid-mid',
                 'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E8%8A%92%E6%9E%9C%E7%B3%AF%E7%B1%B3%E9%A3%AF.mp3',
                 'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/mango%20sticky%20rice.jpg'},
        '炒飯': {'thai': 'ข้าวผัด', 'pronunciation': 'khao-pad', 'tone': 'falling-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%82%92%E9%A3%AF.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/fried%20rice.jpg'},
        '青木瓜沙拉': {'thai': 'ส้มตำ', 'pronunciation': 'som-tam', 'tone': 'falling-mid',
                  'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%9D%92%E6%9C%A8%E7%93%9C%E6%B2%99%E6%8B%89.mp3',
                  'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/papaya-salad.jpg'},
        '冬蔭功湯': {'thai': 'ต้มยำกุ้ง', 'pronunciation': 'tom-yum-kung', 'tone': 'high-mid-mid',
                 'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E5%86%AC%E8%94%AD%E5%8A%9F%E6%B9%AF.mp3',
                 'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/tom%20yam%20kung.jpg'},
        '泰式炒河粉': {'thai': 'ผัดไทย', 'pronunciation': 'pad-thai', 'tone': 'low-mid',
                  'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E6%B3%B0%E5%BC%8F%E7%82%92%E6%B2%B3%E7%B2%89.mp3',
                  'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/pad%20tai.jpg'},
        
        # 交通工具
        '車子': {'thai': 'รถยนต์', 'pronunciation': 'rot-yon', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%BB%8A%E5%AD%90.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E6%B1%BD%E8%BB%8A.jpg'},
        '公車': {'thai': 'รถเมล์', 'pronunciation': 'rot-mae', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E5%85%AC%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E5%85%AC%E8%BB%8A.jpg'},
        '計程車': {'thai': 'แท็กซี่', 'pronunciation': 'taxi', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%A8%88%E7%A8%8B%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%A8%88%E7%A8%8B%E8%BB%8A.jpg'},
        '摩托車': {'thai': 'มอเตอร์ไซค์', 'pronunciation': 'motor-sai', 'tone': 'mid-mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E6%91%A9%E6%89%98%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E6%91%A9%E6%89%98%E8%BB%8A.jpg'},
        '火車': {'thai': 'รถไฟ', 'pronunciation': 'rot-fai', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E7%81%AB%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E7%81%AB%E8%BB%8A.jpg'},
        '飛機': {'thai': 'เครื่องบิน', 'pronunciation': 'krueang-bin', 'tone': 'falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E9%A3%9B%E6%A9%9F.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E9%A3%9B%E6%A9%9F.jpg'},
        '船': {'thai': 'เรือ', 'pronunciation': 'ruea', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%88%B9.jpg',
             'image_url': 'https://storage.googleapis.com/[YOUR_BUCKET]/images/boat.jpg'},
        '腳踏車': {'thai': 'จักรยาน', 'pronunciation': 'jak-ka-yan', 'tone': 'low-low-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%85%B3%E8%B8%8F%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%85%B3%E8%B8%8F%E8%BB%8A.jpg'},
        '嘟嘟車': {'thai': 'ตุ๊กตุ๊ก', 'pronunciation': 'tuk-tuk', 'tone': 'high-high',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E5%98%9F%E5%98%9F%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E5%98%9F%E5%98%9F%E8%BB%8A.jpg'},
        '貨車': {'thai': 'รถบรรทุก', 'pronunciation': 'rot-ban-tuk', 'tone': 'high-mid-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%B2%A8%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%B2%A8%E8%BB%8A.jpg'}
    },
    'tone_guide': {
        'mid': '中調 - 平穩音調',
        'low': '低調 - 以較低音高發音',
        'falling': '降調 - 音調從高降到低',
        'high': '高調 - 以較高音高發音',
        'rising': '升調 - 音調從低升到高'
    },
    'tone_examples': [
        {'thai': 'คา', 'meaning': '卡', 'tone': 'mid', 'pronunciation': 'ka (平穩音)'},
        {'thai': 'ค่า', 'meaning': '價值', 'tone': 'low', 'pronunciation': 'kà (低音)'},
        {'thai': 'ค้า', 'meaning': '貿易', 'tone': 'falling', 'pronunciation': 'kâ (從高到低)'},
        {'thai': 'ค๊า', 'meaning': '(語氣詞)', 'tone': 'high', 'pronunciation': 'ká (高音)'},
        {'thai': 'ค๋า', 'meaning': '(無特定含義)', 'tone': 'rising', 'pronunciation': 'kǎ (從低到高)'}
    ],
    'daily_lessons': [
        {
            'day': 1, 
            'theme': '基本問候',
            'words': ['你好', '謝謝', '再見'],
            'dialogue': None
        },
        {
            'day': 2, 
            'theme': '基本禮貌用語',
            'words': ['對不起', '謝謝', '不客氣'],
            'dialogue': None
        },
        {
            'day': 3, 
            'theme': '購物短語',
            'words': ['多少錢', '好吃', '謝謝'],
            'dialogue': None
        }
    ]
}

logger.info("已載入泰語學習資料")
# === 第三部分：音頻處理和語音評估功能 ===

# === 輔助函數 ===
def get_audio_content(message_id):
    """從LINE取得音訊內容"""
    logger.info(f"獲取音訊內容，訊息ID: {message_id}")
    message_content = line_bot_api.get_message_content(message_id)
    audio_content = b''
    for chunk in message_content.iter_content():
        audio_content += chunk
    return audio_content

def process_audio_content_with_gcs(audio_content, user_id):
    """處理音頻內容並上傳到 GCS"""
    try:
        # 創建臨時目錄
        temp_dir = os.environ.get('TEMP', '/tmp')
        audio_dir = os.path.join(temp_dir, 'temp_audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 生成唯一的文件名
        audio_id = f"{user_id}_{uuid.uuid4()}"
        temp_m4a = os.path.join(audio_dir, f'temp_{audio_id}.m4a')
        temp_wav = os.path.join(audio_dir, f'temp_{audio_id}.wav')
        
        logger.info(f"保存原始音頻到 {temp_m4a}")
        # 保存原始音頻
        with open(temp_m4a, 'wb') as f:
            f.write(audio_content)
        
        logger.info("使用 pydub 轉換音頻格式")
        # 使用 pydub 轉換格式 - 使用 Azure 推薦的格式
        audio = AudioSegment.from_file(temp_m4a)
        audio_wav = audio_m4a.set_frame_rate(16000).set_sample_width(2).set_channels(1)
        audio.export(temp_wav, format='wav')
        
        # 確認 WAV 檔案已成功創建
        if not os.path.exists(temp_wav):
            logger.error(f"WAV 檔案創建失敗: {temp_wav}")
            return None, None
            
        logger.info(f"音頻轉換成功，WAV 檔案路徑: {temp_wav}")
            
        # 上傳到 GCS
        gcs_path = f"user_audio/{audio_id}.wav"
        
        # 重新打開檔案用於上傳（確保檔案指針在起始位置）
        with open(temp_wav, 'rb') as wav_file:
            public_url = upload_file_to_gcs(wav_file, gcs_path, "audio/wav")
        
        # 清除臨時文件（不要清除 temp_wav，因為後續需要使用）
        try:
            os.remove(temp_m4a)
            logger.info(f"已清除臨時文件 {temp_m4a}")
        except Exception as e:
            logger.warning(f"清除臨時文件失敗: {str(e)}")
            pass
        
        # 如果 GCS 上傳失敗，返回本地路徑仍舊有效
        return public_url, temp_wav
    except Exception as e:
        logger.error(f"音頻處理錯誤: {str(e)}")
        return None, None

def process_audio_content_with_gcs(audio_content, user_id):
    """處理音頻內容並上傳到 GCS"""
    try:
        # 創建臨時目錄
        temp_dir = os.environ.get('TEMP', '/tmp')
        audio_dir = os.path.join(temp_dir, 'temp_audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 生成唯一的文件名
        audio_id = f"{user_id}_{uuid.uuid4()}"
        temp_m4a = os.path.join(audio_dir, f'temp_{audio_id}.m4a')
        temp_wav = os.path.join(audio_dir, f'temp_{audio_id}.wav')
        
        logger.info(f"保存原始音頻到 {temp_m4a}")
        # 保存原始音頻
        with open(temp_m4a, 'wb') as f:
            f.write(audio_content)
        
        logger.info("使用 pydub 轉換音頻格式")
        # 使用 pydub 轉換格式
        audio = AudioSegment.from_file(temp_m4a)
        audio = audio.set_frame_rate(16000).set_channels(1)
        audio.export(temp_wav, format='wav')
        
        # 確認 WAV 檔案已成功創建
        if not os.path.exists(temp_wav):
            logger.error(f"WAV 檔案創建失敗: {temp_wav}")
            return None, None
            
        logger.info(f"音頻轉換成功，WAV 檔案路徑: {temp_wav}")
            
        # 上傳到 GCS
        gcs_path = f"user_audio/{audio_id}.wav"
        
        # 重新打開檔案用於上傳（確保檔案指針在起始位置）
        with open(temp_wav, 'rb') as wav_file:
            public_url = upload_file_to_gcs(wav_file, gcs_path, "audio/wav")
        
        # 清除臨時文件（不要清除 temp_wav，因為後續需要使用）
        try:
            os.remove(temp_m4a)
            logger.info(f"已清除臨時文件 {temp_m4a}")
        except Exception as e:
            logger.warning(f"清除臨時文件失敗: {str(e)}")
            pass
        
        # 如果 GCS 上傳失敗，返回本地路徑仍舊有效
        return public_url, temp_wav
    except Exception as e:
        logger.error(f"音頻處理錯誤: {str(e)}")
        return None, None
    
def evaluate_pronunciation(audio_file_path, reference_text, language="th-TH"):
    """使用Azure Speech Services進行發音評估"""
    try:
        logger.info(f"開始發音評估，參考文本: {reference_text}, 音頻檔案: {audio_file_path}")
        
        # 確認檔案存在
        if not os.path.exists(audio_file_path):
            logger.error(f"音頻檔案不存在: {audio_file_path}")
            return {
                "success": False,
                "error": f"音頻檔案不存在: {audio_file_path}"
            }
            
        # 檢查檔案大小
        file_size = os.path.getsize(audio_file_path)
        logger.info(f"音頻檔案大小: {file_size} 字節")
        if file_size == 0:
            logger.error("音頻檔案為空")
            return {
                "success": False,
                "error": "音頻檔案為空"
            }
            
        # 設定語音配置
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        speech_config.speech_recognition_language = language
        
        logger.info("已設置 Speech Config")
        
        # 設定發音評估配置
        pronunciation_config = speechsdk.PronunciationAssessmentConfig(
            reference_text=reference_text,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.FullText,
            enable_miscue=True
        )
        
        logger.info("已設置發音評估配置")
        
        # 設定音訊輸入 - 使用絕對路徑
        abs_path = os.path.abspath(audio_file_path)
        logger.info(f"音頻檔案絕對路徑: {abs_path}")
        audio_config = speechsdk.audio.AudioConfig(filename=abs_path)
        
        logger.info("已設置音訊輸入配置")
        
        # 創建語音識別器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        logger.info("已創建語音識別器")
        
        # 設置錯誤回調以獲取更詳細的錯誤信息
        done = False
        error_details = ""
        
        def recognized_cb(evt):
            logger.info(f"RECOGNIZED: {evt}")
        
        def canceled_cb(evt):
            nonlocal done, error_details
            logger.info(f"CANCELED: {evt}")
            if evt.reason == speechsdk.CancellationReason.Error:
                logger.error(f"錯誤碼: {evt.error_code}")
                logger.error(f"錯誤詳情: {evt.error_details}")
                error_details = f"錯誤碼: {evt.error_code}, 錯誤詳情: {evt.error_details}"
            done = True
        
        # 添加回調
        speech_recognizer.recognized.connect(recognized_cb)
        speech_recognizer.canceled.connect(canceled_cb)
        
        # 應用發音評估配置
        pronunciation_assessment = pronunciation_config.apply_to(speech_recognizer)
        
        # 開始識別
        logger.info("開始識別語音...")
        result = speech_recognizer.recognize_once_async().get()
        
        if error_details:
            logger.error(f"詳細錯誤信息: {error_details}")
        
        # 處理結果
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            pronunciation_result = speechsdk.PronunciationAssessmentResult(result)
            
            # 獲取評估結果
            accuracy_score = pronunciation_result.accuracy_score
            pronunciation_score = pronunciation_result.pronunciation_score
            completeness_score = pronunciation_result.completeness_score
            fluency_score = pronunciation_result.fluency_score
            
            # 計算總分
            overall_score = int((accuracy_score + pronunciation_score + completeness_score + fluency_score) / 4)
            
            logger.info(f"發音評估完成，總分: {overall_score}, 識別文字: {result.text}")
            return {
                "success": True,
                "recognized_text": result.text,
                "reference_text": reference_text,
                "overall_score": overall_score,
                "accuracy_score": accuracy_score,
                "pronunciation_score": pronunciation_score,
                "completeness_score": completeness_score,
                "fluency_score": fluency_score
            }
        else:
            # 更安全的錯誤處理方式
            try:
                detail_info = ""
                if result.reason == speechsdk.ResultReason.Canceled:
                    cancellation = result.cancellation_details
                    cancellation_reason = f"{cancellation.reason}"
                    if cancellation.reason == speechsdk.CancellationReason.Error:
                        # 安全地訪問屬性
                        if hasattr(cancellation, 'error_code'):
                            detail_info += f"錯誤碼: {cancellation.error_code}"
                        if hasattr(cancellation, 'error_details'):
                            detail_info += f", 錯誤詳情: {cancellation.error_details}"
                        logger.error(detail_info)
                    else:
                        detail_info = f"取消原因: {cancellation_reason}"
                
                logger.warning(f"語音識別失敗，原因: {result.reason}, 詳細資訊: {detail_info or '無詳細資訊'}")
                
                # 鑑於 Azure 似乎不支援泰語的發音評估，使用模擬評估
                logger.info("切換到模擬評估模式")
                return simulate_pronunciation_assessment(audio_file_path, reference_text)
            
            except Exception as e:
                logger.error(f"錯誤處理過程中發生異常: {str(e)}", exc_info=True)
                # 出現例外時依然使用模擬評估
                logger.info("因錯誤處理異常切換到模擬評估模式")
                return simulate_pronunciation_assessment(audio_file_path, reference_text)
    
    except Exception as e:
        logger.error(f"發音評估過程中發生錯誤: {str(e)}", exc_info=True)
        # 發生錯誤時也使用模擬評估
        logger.info("因發音評估錯誤切換到模擬評估模式")
        return simulate_pronunciation_assessment(audio_file_path, reference_text)
       
    finally:
        # 保留臨時檔案以便調試
        # 在問題排除後，可以重新啟用此代碼以清理臨時檔案
        # try:
        #     if os.path.exists(audio_file_path):
        #         os.remove(audio_file_path)
        #         logger.info(f"已清除臨時檔案 {audio_file_path}")
        # except Exception as e:
        #     logger.warning(f"清除臨時檔案失敗: {str(e)}")
        pass
    
def get_audio_content_with_gcs(message_id, user_id):
    """從LINE取得音訊內容並存儲到 GCS"""
    logger.info(f"獲取音訊內容，訊息ID: {message_id}")
    try:
        message_content = line_bot_api.get_message_content(message_id)
        audio_content = b''
        for chunk in message_content.iter_content():
            audio_content += chunk
        
        logger.info(f"成功獲取音訊內容，大小: {len(audio_content)} 字節")
        
        # 上傳到 GCS
        public_url, temp_file = process_audio_content_with_gcs(audio_content, user_id)
        
        if not public_url:
            logger.warning("GCS 上傳失敗，但本地文件可能仍然可用")
        
        if not temp_file:
            logger.error("音頻處理失敗，無法獲取本地文件路徑")
            
        return audio_content, public_url, temp_file
    except Exception as e:
        logger.error(f"獲取音訊內容時發生錯誤: {str(e)}", exc_info=True)
        return None, None, None
        
        # 確認檔案存在
        if not os.path.exists(audio_file_path):
            logger.error(f"音頻檔案不存在: {audio_file_path}")
            return {
                "success": False,
                "error": f"音頻檔案不存在: {audio_file_path}"
            }
            
        # 檢查檔案大小
        file_size = os.path.getsize(audio_file_path)
        logger.info(f"音頻檔案大小: {file_size} 字節")
        if file_size == 0:
            logger.error("音頻檔案為空")
            return {
                "success": False,
                "error": "音頻檔案為空"
            }
            
        # 設定語音配置
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        speech_config.speech_recognition_language = language
        
        logger.info("已設置 Speech Config")
        
        # 設定發音評估配置
        pronunciation_config = speechsdk.PronunciationAssessmentConfig(
            reference_text=reference_text,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
            enable_miscue=True
        )
        
        logger.info("已設置發音評估配置")
        
        # 設定音訊輸入 - 使用絕對路徑
        abs_path = os.path.abspath(audio_file_path)
        logger.info(f"音頻檔案絕對路徑: {abs_path}")
        audio_config = speechsdk.audio.AudioConfig(filename=abs_path)
        
        logger.info("已設置音訊輸入配置")
        
        # 創建語音識別器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        logger.info("已創建語音識別器")
        
        # 應用發音評估配置
        pronunciation_assessment = pronunciation_config.apply_to(speech_recognizer)
        
        # 開始識別
        logger.info("開始識別語音...")
        result = speech_recognizer.recognize_once_async().get()
        
        # 處理結果
        if result.reason == speechsdk.ResultReason.RecognizedSpeech:
            pronunciation_result = speechsdk.PronunciationAssessmentResult(result)
            
            # 獲取評估結果
            accuracy_score = pronunciation_result.accuracy_score
            pronunciation_score = pronunciation_result.pronunciation_score
            completeness_score = pronunciation_result.completeness_score
            fluency_score = pronunciation_result.fluency_score
            
            # 計算總分
            overall_score = int((accuracy_score + pronunciation_score + completeness_score + fluency_score) / 4)
            
            logger.info(f"發音評估完成，總分: {overall_score}, 識別文字: {result.text}")
            return {
                "success": True,
                "recognized_text": result.text,
                "reference_text": reference_text,
                "overall_score": overall_score,
                "accuracy_score": accuracy_score,
                "pronunciation_score": pronunciation_score,
                "completeness_score": completeness_score,
                "fluency_score": fluency_score
            }
        else:
            logger.warning(f"語音識別失敗，原因: {result.reason}, 詳細資訊: {result.cancellation_details.reason if hasattr(result, 'cancellation_details') else 'None'}")
            return {
                "success": False,
                "error": f"無法識別語音，原因: {result.reason}",
                "result_reason": result.reason,
                "details": result.cancellation_details.reason if hasattr(result, 'cancellation_details') else 'None'
            }
    
    except Exception as e:
        logger.error(f"發音評估過程中發生錯誤: {str(e)}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        # 清理臨時檔案 - 但保留日誌
        try:
            # 不要立即刪除臨時檔案，可能需要進一步調試
            # 可以在問題排除後重新添加這段代碼
            # if os.path.exists(audio_file_path):
            #     os.remove(audio_file_path)
            #     logger.info(f"已清除臨時檔案 {audio_file_path}")
            pass
        except Exception as e:
            logger.warning(f"清除臨時檔案失敗: {str(e)}")
            pass


@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    """處理音頻消息，主要用於發音評估"""
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的音頻訊息")
    
    # 檢查用戶是否在發音練習中
    if user_data.get('current_activity') == 'echo_practice':
        try:
            # 獲取音訊內容並上傳到 GCS
            audio_content, gcs_url, audio_file_path = get_audio_content_with_gcs(event.message.id, user_id)
            
            # 檢查音頻檔案路徑是否有效
            if not audio_file_path or not os.path.exists(audio_file_path):
                logger.error(f"音頻檔案路徑無效: {audio_file_path}")
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="處理您的錄音時發生錯誤，請重新嘗試。")
                )
                return
                
            # 獲取當前詞彙
            word_key = user_data.get('current_vocab')
            if not word_key:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="請先選擇一個詞彙進行學習")
                )
                return
                
            word_data = thai_data['basic_words'][word_key]
            logger.info(f"正在評估用戶 {user_id} 的 '{word_key}' ({word_data['thai']}) 發音")
            
            # 使用Azure評估發音
            assessment_result = evaluate_pronunciation(
                audio_file_path, 
                word_data['thai'],
                language="th-TH"
            )
            
            # 準備回應訊息
            if assessment_result["success"]:
                score = assessment_result["overall_score"]
                
                # 根據分數生成反饋
                if score >= 90:
                    feedback = f"太棒了！您的 '{word_key}' ({word_data['thai']}) 發音非常準確。"
                elif score >= 75:
                    feedback = f"做得好！您的 '{word_key}' ({word_data['thai']}) 發音清晰，繼續保持。"
                elif score >= 60:
                    feedback = f"不錯的嘗試。您的 '{word_key}' ({word_data['thai']}) 發音基本正確，但可以再加強音調。"
                else:
                    feedback = f"繼續練習！注意 '{word_key}' ({word_data['thai']}) 的音調變化，可多聽幾次標準發音。"
                
                # 更新用戶學習進度
                if 'vocab_mastery' not in user_data:
                    user_data['vocab_mastery'] = {}
                
                if word_key not in user_data['vocab_mastery']:
                    user_data['vocab_mastery'][word_key] = {
                        'practice_count': 1,
                        'scores': [score],
                        'last_practiced': datetime.now().strftime("%Y-%m-%d"),
                        'audio_url': gcs_url  # 保存用戶的音頻 URL
                    }
                else:
                    user_data['vocab_mastery'][word_key]['practice_count'] += 1
                    user_data['vocab_mastery'][word_key]['scores'].append(score)
                    user_data['vocab_mastery'][word_key]['last_practiced'] = datetime.now().strftime("%Y-%m-%d")
                    user_data['vocab_mastery'][word_key]['audio_url'] = gcs_url  # 更新音頻 URL
                
                logger.info(f"用戶 {user_id} 的 '{word_key}' 發音評分: {score}")
                
                # 詳細評分內容
                details = f"發音評估詳情：\n" \
                         f"整體評分：{score}/100\n" \
                         f"準確度：{assessment_result['accuracy_score']}/100\n" \
                         f"發音清晰度：{assessment_result['pronunciation_score']}/100\n" \
                         f"完整度：{assessment_result['completeness_score']}/100\n" \
                         f"流暢度：{assessment_result['fluency_score']}/100"
                
                # 建立回覆訊息
                messages = [
                    TextSendMessage(text=f"發音評分：{score}/100"),
                    TextSendMessage(text=feedback),
                    TextSendMessage(text=details)
                ]
                
                # 添加選項按鈕
                buttons_template = ButtonsTemplate(
                    title="發音評估結果",
                    text="請選擇下一步",
                    actions=[
                        MessageAction(label="再次練習", text="練習發音"),
                        MessageAction(label="下一個詞彙", text="下一個詞彙"),
                        MessageAction(label="返回主選單", text="返回主選單")
                    ]
                )
                messages.append(
                    TemplateSendMessage(alt_text="發音評估選項", template=buttons_template)
                )
                
                line_bot_api.reply_message(event.reply_token, messages)
            else:
                # 發生錯誤
                error_msg = assessment_result.get("error", "未知錯誤")
                logger.error(f"發音評估失敗: {error_msg}")
                line_bot_api.reply_message(
                    event.reply_token,
                    [
                        TextSendMessage(text=f"發音評估失敗：{error_msg}"),
                        TextSendMessage(text="請重新嘗試發音，或選擇其他詞彙學習")
                    ]
                )
        except Exception as e:
            # 改善錯誤訊息
            logger.error(f"處理音頻時發生錯誤: {str(e)}", exc_info=True)  # 添加完整的錯誤堆疊追蹤
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"處理您的錄音時發生錯誤。我們已記錄此問題並會盡快修復。請重新嘗試。")
            )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請先選擇「練習發音」開始發音練習")
        )
        # === 第四部分：學習功能模塊 ===

# === 學習功能和選單 ===
def show_category_menu():
    """顯示主題選單"""
    logger.info("顯示主題選單")
    
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='日常用語', text='主題:日常用語')),
            QuickReplyButton(action=MessageAction(label='數字', text='主題:數字')),
            QuickReplyButton(action=MessageAction(label='動物', text='主題:動物')),
            QuickReplyButton(action=MessageAction(label='食物', text='主題:食物')),
            QuickReplyButton(action=MessageAction(label='交通工具', text='主題:交通工具'))
        ]
    )
    
    return TextSendMessage(
        text="請選擇您想學習的主題：",
        quick_reply=quick_reply
    )

def start_image_learning(user_id, category=None):
    """啟動圖像詞彙學習模式"""
    logger.info(f"啟動圖像詞彙學習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'image_learning'
    
    # 如果指定了主題，設置當前主題
    if category:
        user_data['current_category'] = category
        word_key = random.choice(thai_data['categories'][category]['words'])
    # 隨機選擇詞彙，或從之前的主題中選擇
    elif user_data.get('current_vocab'):
        word_key = user_data['current_vocab']
    else:
        # 如果有當前主題，從該主題中選詞
        if user_data.get('current_category'):
            category = user_data['current_category']
            word_key = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則隨機選擇一個詞
            word_key = random.choice(list(thai_data['basic_words'].keys()))
    
    user_data['current_vocab'] = word_key
    word_data = thai_data['basic_words'][word_key]
    logger.info(f"選擇詞彙: {word_key}, 泰語: {word_data['thai']}")
    
    # 建立訊息列表
    message_list = []
    
    # 添加圖片
    if 'image_url' in word_data and word_data['image_url']:
        message_list.append(
            ImageSendMessage(
                original_content_url=word_data['image_url'],
                preview_image_url=word_data['image_url']
            )
        )
    
    # 添加詞彙訊息
    message_list.append(
        TextSendMessage(
            text=f"泰語：{word_data['thai']}\n中文：{word_key}\n發音：{word_data['pronunciation']}\n音調：{word_data['tone']}"
        )
    )
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="詞彙學習",
        text="請選擇下一步",
        actions=[
            MessageAction(label="練習發音", text="練習發音"),
            MessageAction(label="下一個詞彙", text="下一個詞彙"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="詞彙學習選項", template=buttons_template)
    )
    
    return message_list

def start_echo_practice(user_id):
    """啟動回音法發音練習"""
    logger.info(f"啟動回音法發音練習，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'echo_practice'

    # 獲取當前詞彙，若無則隨機選擇
    if not user_data.get('current_vocab'):
        # 如果有當前主題，從該主題中選詞
        if user_data.get('current_category'):
            category = user_data['current_category']
            word_key = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則隨機選擇一個詞
            word_key = random.choice(list(thai_data['basic_words'].keys()))
        user_data['current_vocab'] = word_key
    
    word_key = user_data['current_vocab']
    word_data = thai_data['basic_words'][word_key]
    logger.info(f"發音練習詞彙: {word_key}, 泰語: {word_data['thai']}")
    
    # 建立訊息列表
    message_list = []
    
    # 添加音訊提示
    if 'audio_url' in word_data and word_data['audio_url']:
        message_list.append(
            AudioSendMessage(
                original_content_url=word_data['audio_url'],
                duration=3000  # 假設音訊長度為3秒
            )
        )
    
    # 添加發音指導
    message_list.append(
        TextSendMessage(
            text=f"請聽標準發音，然後跟著練習：\n\n泰語：{word_data['thai']}\n發音：{word_data['pronunciation']}\n\n請點擊聊天室底部的麥克風圖標(🎤)錄製您的發音"
        )
    )
    
    # 添加音調指導
    tone_info = ""
    for part in word_data['tone'].split('-'):
        if part in thai_data['tone_guide']:
            tone_info += thai_data['tone_guide'][part] + "\n"
    
    message_list.append(
        TextSendMessage(text=f"音調指南：\n{tone_info}")
    )
    
    # 添加選項按鈕（移除錄音按鈕，因為會使用LINE聊天界面的麥克風按鈕）
    buttons_template = ButtonsTemplate(
        title="發音練習",
        text="其他選項",
        actions=[
            MessageAction(label="再聽一次", text=f"播放音頻:{word_key}"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="發音練習", template=buttons_template)
    )
    
    return message_list

def start_tone_learning(user_id):
    """啟動音調學習模式"""
    logger.info(f"啟動音調學習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'tone_learning'
    
    # 建立訊息列表
    message_list = []
    
    # 泰語音調介紹
    message_list.append(
        TextSendMessage(
            text="泰語有五種音調，不同音調會改變詞義：\n\n1. 中調 (無標記)\n2. 低調 (่)\n3. 降調 (้)\n4. 高調 (๊)\n5. 升調 (๋)"
        )
    )
    
    # 提供音調例子
    examples_text = "音調例子：\n\n"
    for example in thai_data['tone_examples']:
        examples_text += f"{example['thai']} - {example['meaning']} - {example['pronunciation']} ({example['tone']}調)\n"
    
    message_list.append(TextSendMessage(text=examples_text))
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="音調學習",
        text="請選擇操作",
        actions=[
            MessageAction(label="練習發音", text="練習發音"),
            MessageAction(label="詞彙學習", text="詞彙學習"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="音調學習選項", template=buttons_template)
    )
    
    return message_list

def show_learning_progress(user_id):
    """顯示用戶學習進度"""
    logger.info(f"顯示學習進度，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    
    # 檢查是否有學習記錄
    if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
        return TextSendMessage(text="您還沒有開始學習。請選擇「詞彙學習」或「發音練習」開始您的泰語學習之旅！")
    
    # 統計學習數據
    total_words = len(user_data['vocab_mastery'])
    total_practices = sum(data['practice_count'] for data in user_data['vocab_mastery'].values())
    
    # 計算平均分數
    all_scores = []
    for data in user_data['vocab_mastery'].values():
        all_scores.extend(data['scores'])
    
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    
    # 找出最佳和需要改進的詞彙
    if total_words > 0:
        best_word = max(user_data['vocab_mastery'].items(), 
                         key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 0)
        worst_word = min(user_data['vocab_mastery'].items(), 
                          key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
    
    # 格式化進度報告
    progress_report = f"學習進度報告：\n\n"
    progress_report += f"已學習詞彙：{total_words} 個\n"
    progress_report += f"練習次數：{total_practices} 次\n"
    progress_report += f"平均發音評分：{avg_score:.1f}/100\n"
    progress_report += f"學習連續天數：{user_data['streak']} 天\n\n"
    
    if total_words > 0:
        progress_report += f"最佳發音詞彙：{best_word[0]} ({thai_data['basic_words'][best_word[0]]['thai']})\n"
        progress_report += f"需要加強的詞彙：{worst_word[0]} ({thai_data['basic_words'][worst_word[0]]['thai']})"
    
    # 添加進度按鈕
    buttons_template = ButtonsTemplate(
        title="學習進度",
        text="選擇下一步",
        actions=[
            MessageAction(label="練習弱點詞彙", text="練習弱點"),
            MessageAction(label="查看學習日曆", text="學習日曆"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    
    return [
        TextSendMessage(text=progress_report),
        TemplateSendMessage(alt_text="學習進度選項", template=buttons_template)
    ]

def show_main_menu():
    """顯示主選單"""
    logger.info("顯示主選單")
    
    # 使用 QuickReply 代替 ButtonsTemplate，因為 QuickReply 可以支援更多按鈕
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='選擇主題', text='選擇主題')),
            QuickReplyButton(action=MessageAction(label='詞彙學習', text='詞彙學習')),
            QuickReplyButton(action=MessageAction(label='發音練習', text='練習發音')),
            QuickReplyButton(action=MessageAction(label='音調學習', text='音調學習')),
            QuickReplyButton(action=MessageAction(label='記憶遊戲', text='開始記憶遊戲')),
            QuickReplyButton(action=MessageAction(label='學習進度', text='學習進度'))
        ]
    )
    
    return TextSendMessage(
        text="🇹🇭 歡迎使用泰語學習系統 🇹🇭\n請選擇您想要的學習模式：",
        quick_reply=quick_reply
    )
# === 第五部分：記憶翻牌遊戲和訊息處理 ===
from linebot.models import (
    FlexSendMessage, BubbleContainer, BoxComponent, TextComponent, ButtonComponent,
    ImageComponent, IconComponent, SeparatorComponent, URIAction, MessageAction, PostbackAction
)

# === 記憶翻牌遊戲類 ===
class MemoryGame:
    def __init__(self, category=None):
        """初始化記憶翻牌遊戲"""
        self.cards = []
        self.flipped_cards = []
        self.matched_pairs = []
        self.attempts = 0
        self.start_time = None
        self.end_time = None
        self.category = category
        self.time_limit = 90  # 設定時間限制為90秒（1分30秒）
        self.pending_reset = False  # 用於配對失敗時，暫時保持卡片翻開
        
    def initialize_game(self, category=None):
        """根據類別初始化遊戲卡片"""
        if category:
            self.category = category
        
        # 如果沒有指定類別，隨機選擇一個
        if not self.category:
            self.category = random.choice(list(thai_data['categories'].keys()))
        
        # 從類別中選擇 5 個詞彙
        category_words = thai_data['categories'][self.category]['words']
        
        # 如果是食物類別，確保不包含有問題的詞彙
        if self.category == 'food' and '麵' in category_words:
            category_words.remove('麵')
            logger.info("從食物類別中移除了詞彙「麵」")
        
        selected_words = random.sample(category_words, min(5, len(category_words)))
        
        # 初始化卡片清單
        self.cards = []
        card_id = 1
        
        # 為每個詞彙創建一對卡片（圖片卡和音頻卡）
        for word in selected_words:
            word_data = thai_data['basic_words'][word]
            
            # 添加圖片卡
            self.cards.append({
                'id': card_id,
                'type': 'image',
                'content': word_data['image_url'],
                'match_id': card_id + 1,
                'word': word,
                'meaning': word,
                'thai': word_data['thai']
            })
            card_id += 1
            
            # 添加音頻卡
            self.cards.append({
                'id': card_id,
                'type': 'audio',
                'content': word_data['audio_url'],
                'match_id': card_id - 1,
                'word': word,
                'meaning': word,
                'thai': word_data['thai']
            })
            card_id += 1
        
        # 洗牌
        random.shuffle(self.cards)
        
        # 重置遊戲狀態
        self.flipped_cards = []
        self.matched_pairs = []
        self.attempts = 0
        self.start_time = datetime.now()
        self.end_time = None
        self.pending_reset = False
        
        logger.info(f"初始化記憶翻牌遊戲，類別: {self.category}，卡片數量: {len(self.cards)}")
        return self.cards
    
    def flip_card(self, card_id):
        """翻轉卡片並檢查配對"""
        # 檢查是否需要重置先前不匹配的卡片
        if self.pending_reset:
            logger.info("重置先前不匹配的卡片")
            self.flipped_cards = []
            self.pending_reset = False
        
        # 尋找卡片
        card = next((c for c in self.cards if c['id'] == card_id), None)
        if not card:
            logger.warning(f"找不到卡片 ID: {card_id}")
            return None, "卡片不存在", False, None
        
        # 檢查卡片是否已經配對
        if card_id in [c['id'] for pair in self.matched_pairs for c in pair]:
            logger.warning(f"卡片 {card_id} 已經配對")
            return self.get_game_state(), "卡片已經配對", False, None
        
        # 檢查卡片是否已經翻轉
        if card_id in [c['id'] for c in self.flipped_cards]:
            logger.warning(f"卡片 {card_id} 已經翻轉")
            return self.get_game_state(), "卡片已經翻轉", False, None
        
        # 添加到翻轉卡片列表
        self.flipped_cards.append(card)
        
        # 檢查是否需要播放音頻
        should_play_audio = False
        audio_url = None
        if card['type'] == 'audio':
            should_play_audio = True
            word = card['word']
            if word in thai_data['basic_words'] and 'audio_url' in thai_data['basic_words'][word]:
                audio_url = thai_data['basic_words'][word]['audio_url']
        
        # 如果翻轉了兩張卡片，檢查是否匹配
        result = "繼續遊戲"
        if len(self.flipped_cards) == 2:
            self.attempts += 1
            card1, card2 = self.flipped_cards
            
            # 檢查是否配對
            if card1['match_id'] == card2['id'] and card2['match_id'] == card1['id']:
                # 配對成功
                self.matched_pairs.append(self.flipped_cards.copy())
                result = f"配對成功！{card1['word']} - {card1['thai']}"
                logger.info(f"卡片配對成功: {card1['id']} 和 {card2['id']}")
                # 配對成功才清空翻轉卡片列表
                self.flipped_cards = []
            else:
                # 配對失敗 - 設置標記而不是立即清空翻轉卡片列表
                result = "配對失敗，請再試一次"
                logger.info(f"卡片配對失敗: {card1['id']} 和 {card2['id']}")
                self.pending_reset = True
                # 不要在這裡清空 self.flipped_cards，這樣卡片會保持翻開狀態
        
        # 檢查遊戲是否結束
        if len(self.matched_pairs) * 2 == len(self.cards):
            self.end_time = datetime.now()
            result = self.get_end_result()
            logger.info("記憶翻牌遊戲結束")
        
        # 檢查是否超時
        elif self.start_time:
            elapsed_time = (datetime.now() - self.start_time).total_seconds()
            if elapsed_time > self.time_limit:
                self.end_time = datetime.now()
                result = "時間到！" + self.get_end_result()
                logger.info("記憶翻牌遊戲超時")
        
        return self.get_game_state(), result, should_play_audio, audio_url
    
    def get_game_state(self):
        """獲取當前遊戲狀態"""
        elapsed_time = 0
        if self.start_time:
            current_time = self.end_time if self.end_time else datetime.now()
            elapsed_time = (current_time - self.start_time).total_seconds()
        
        remaining_time = max(0, self.time_limit - elapsed_time)
        
        # 計算類別名稱
        category_name = ""
        if self.category and self.category in thai_data['categories']:
            category_name = thai_data['categories'][self.category]['name']
        
        return {
            'cards': self.cards,
            'flipped_cards': [c['id'] for c in self.flipped_cards],
            'matched_pairs': [[c['id'] for c in pair] for pair in self.matched_pairs],
            'attempts': self.attempts,
            'elapsed_time': elapsed_time,
            'remaining_time': remaining_time,
            'is_completed': len(self.matched_pairs) * 2 == len(self.cards),
            'is_timeout': elapsed_time > self.time_limit,
            'category': self.category,
            'category_name': category_name,
            'pending_reset': self.pending_reset
        }
    
    def get_end_result(self):
        """獲取遊戲結束結果"""
        if not self.end_time:
            return "遊戲尚未結束"
        
        duration = (self.end_time - self.start_time).total_seconds()
        pairs_count = len(self.cards) // 2
        matched_count = len(self.matched_pairs)
        
        # 計算分數和等級
        if duration > self.time_limit:
            # 超時情況
            if matched_count == pairs_count:
                message = "雖然超時，但你找到了所有配對！"
                level = "不錯的嘗試！"
            else:
                message = f"時間到！你找到了 {matched_count}/{pairs_count} 組配對。"
                level = "再接再厲！"
        else:
            # 未超時情況
            if duration < 30:  # 30秒內完成
                level = "太棒了！你的記憶力超群！"
            elif duration < 60:  # 60秒內完成
                level = "很好！你的記憶力很強！"
            else:
                level = "做得好！繼續練習能提高記憶力！"
                
            message = f"遊戲完成！\n配對數量: {matched_count}/{pairs_count} 組\n嘗試次數: {self.attempts} 次\n用時: {int(duration)} 秒"
        
        return f"{message}\n{level}"

# === 記憶翻牌遊戲處理 ===
def handle_memory_game(user_id, message):
    """處理記憶翻牌遊戲訊息"""
    user_data = user_data_manager.get_user_data(user_id)
    
    # 初始化遊戲狀態
    if 'game_state' not in user_data:
        user_data['game_state'] = {}
    
    # 檢查是否有活動的遊戲
    if 'memory_game' not in user_data['game_state']:
        user_data['game_state']['memory_game'] = MemoryGame()
    
    game = user_data['game_state']['memory_game']
    
    # 處理遊戲指令
    if message == "開始記憶遊戲":
        # 顯示主題選單
        quick_reply = QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label='日常用語', text='記憶遊戲主題:日常用語')),
                QuickReplyButton(action=MessageAction(label='數字', text='記憶遊戲主題:數字')),
                QuickReplyButton(action=MessageAction(label='動物', text='記憶遊戲主題:動物')),
                QuickReplyButton(action=MessageAction(label='食物', text='記憶遊戲主題:食物')),
                QuickReplyButton(action=MessageAction(label='交通工具', text='記憶遊戲主題:交通工具'))
            ]
        )
        
        return TextSendMessage(
            text="🎮 記憶翻牌遊戲\n\n遊戲規則：\n1. 翻開卡片找出配對的圖片和發音\n2. 遊戲時間限制為1分30秒\n3. 完成速度越快評價越高\n\n請選擇一個主題開始遊戲：",
            quick_reply=quick_reply
        )
    
    elif message.startswith("記憶遊戲主題:"):
        category = message.split(":", 1)[1] if ":" in message else ""
        logger.info(f"收到記憶遊戲主題選擇: '{category}'")
        
        # 轉換成英文鍵值
        category_map = {
            "日常用語": "daily_phrases",
            "數字": "numbers",
            "動物": "animals",
            "食物": "food",
            "交通工具": "transportation"
        }
        logger.info(f"可用的主題映射: {list(category_map.keys())}")
        
        if category in category_map:
            eng_category = category_map[category]
            logger.info(f"主題映射成功: {category} -> {eng_category}")
            
            # 檢查 thai_data 是否包含該類別
            if eng_category in thai_data['categories']:
                logger.info(f"在 thai_data 中找到類別 {eng_category}")
                # 初始化遊戲
                cards = game.initialize_game(eng_category)
                
                # 創建遊戲畫面 (使用 Flex Message)
                return create_flex_memory_game(cards, game.get_game_state(), user_id)
            else:
                logger.error(f"在 thai_data 中找不到類別 {eng_category}")
                return TextSendMessage(text=f"抱歉，在資料中找不到「{category}」類別。請聯繫管理員。")
        else:
            logger.warning(f"無法識別主題: {category}")
            return TextSendMessage(text="抱歉，無法識別該主題。請重新選擇。")
    
    elif message.startswith("翻牌:"):
        try:
            card_id = int(message.split(":")[1]) if ":" in message else -1
            logger.info(f"用戶點擊卡片號碼: {card_id}")
            
            # 翻開卡片
            game_state, result, should_play_audio, audio_url = game.flip_card(card_id)
            
            # 儲存臨時數據用於訪問遊戲結果
            temp_data = user_data_manager.get_user_data('temp')
            if 'game_state' not in temp_data:
                temp_data['game_state'] = {}
            temp_data['game_state']['memory_game'] = game
            
            # 準備回應訊息
            messages = []
            
            # 添加文字結果
            messages.append(TextSendMessage(text=result))
            
            # 如果需要播放音頻，添加音頻消息
            if should_play_audio and audio_url:
                logger.info(f"準備播放音頻: {audio_url}")
                messages.append(
                    AudioSendMessage(
                        original_content_url=audio_url,
                        duration=3000  # 假設音訊長度為3秒
                    )
                )
            
            # 如果遊戲還在進行中且沒有超時
            if game_state and not game_state.get('is_completed', False) and not game_state.get('is_timeout', False):
                # 返回更新後的遊戲畫面
                messages.append(create_flex_memory_game(game.cards, game_state, user_id))
                return messages
            else:
                # 遊戲結束或超時，顯示結果
                messages.append(
                    TextSendMessage(
                        text="遊戲結束！要再玩一次嗎？",
                        quick_reply=QuickReply(
                            items=[
                                QuickReplyButton(action=MessageAction(label='再玩一次', text='開始記憶遊戲')),
                                QuickReplyButton(action=MessageAction(label='返回主選單', text='返回主選單'))
                            ]
                        )
                    )
                )
                return messages
        except Exception as e:
            logger.error(f"處理翻牌請求時發生錯誤: {str(e)}")
            return TextSendMessage(text=f"處理翻牌請求時發生錯誤: {str(e)}\n請重試或選擇「返回主選單」。")
    
    elif message.startswith("播放音頻:"):
        word = message.split(":", 1)[1] if ":" in message else ""
        if word in thai_data['basic_words']:
            word_data = thai_data['basic_words'][word]
            if 'audio_url' in word_data and word_data['audio_url']:
                # 獲取遊戲狀態
                game_state = game.get_game_state()
                
                # 發送音頻後顯示遊戲畫面
                messages = [
                    AudioSendMessage(
                        original_content_url=word_data['audio_url'],
                        duration=3000  # 假設音訊長度為3秒
                    ),
                    create_flex_memory_game(game.cards, game_state, user_id)
                ]
                return messages
        
        return TextSendMessage(text="抱歉，無法播放該音頻。")
    
    # 默認回傳
    return TextSendMessage(text="請選擇「開始記憶遊戲」開始新的遊戲")

def create_flex_memory_game(cards, game_state, user_id):
    """創建 Flex Message 的記憶翻牌遊戲界面"""
    # 初始化 bubbles 為空列表
    bubbles = []

    try:
        # 遊戲狀態數據
        attempts = game_state.get('attempts', 0)
        remaining_time = int(game_state.get('remaining_time', 0))
        category_name = game_state.get('category_name', '未知')
        is_completed = game_state.get('is_completed', False)
        is_timeout = game_state.get('is_timeout', False)
        
        # 獲取已匹配和已翻開的卡片
        matched_ids = []
        for pair in game_state.get('matched_pairs', []):
            matched_ids.extend(pair)
        flipped_ids = game_state.get('flipped_cards', [])

        # 1. 遊戲信息氣泡
        info_bubble = {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "text",
                        "text": "泰語記憶翻牌遊戲",
                        "weight": "bold",
                        "size": "xl",
                        "color": "#ffffff"
                    },
                    {
                        "type": "text",
                        "text": category_name,
                        "size": "md",
                        "color": "#ffffff"
                    }
                ],
                "backgroundColor": "#4A86E8",
                "paddingBottom": "10px"
            },
            "body": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {
                        "type": "box",
                        "layout": "horizontal",
                        "contents": [
                            {
                                "type": "text",
                                "text": "⏱️ 剩餘時間:",
                                "size": "sm",
                                "color": "#555555",
                                "flex": 2
                            },
                            {
                                "type": "text",
                                "text": f"{remaining_time} 秒",
                                "size": "sm",
                                "color": "#111111",
                                "flex": 1
                            }
                        ]
                    }
                ]
            }
        }
        bubbles.append(info_bubble)

        # 2. 遊戲結束氣泡 (如果適用)
        if is_completed or is_timeout:
            game = next((g for g in [user_data_manager.get_user_data('temp')['game_state'].get('memory_game')] if g), None)
            end_message = game.get_end_result() if game else "遊戲結束！"
            
            end_bubble = {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "vertical",
                    "contents": [
                        {
                            "type": "text",
                            "text": "遊戲結束",
                            "weight": "bold",
                            "size": "xl",
                            "align": "center"
                        },
                        {
                            "type": "text",
                            "text": end_message,
                            "wrap": True,
                            "margin": "md"
                        }
                    ]
                }
            }
            bubbles.append(end_bubble)

        # 3. 卡片氣泡 - 這裡需要修改
        card_rows = [[], []]
        for i, card in enumerate(cards):
            row_index = i // 5
            if row_index < 2:
                card_rows[row_index].append(card)

        for row_cards in card_rows:
            card_contents = []
            for card in row_cards:
                card_id = card['id']
                is_matched = card_id in matched_ids
                is_flipped = card_id in flipped_ids

                # 這裡需要修改 - 根據卡片類型顯示不同內容
                if is_matched or is_flipped:
                    # 已翻開的卡片
                    if card['type'] == 'image':
                        # 圖片卡 - 顯示實際圖片
                        card_box = {
                            "type": "box",
                            "layout": "vertical",
                            "width": "60px",
                            "height": "80px",
                            "backgroundColor": "#E6F5FF",
                            "cornerRadius": "4px",
                            "borderWidth": "1px",
                            "borderColor": "#AAAAAA",
                            "contents": [
                                {
                                    "type": "image",
                                    "url": card['content'],  # 直接使用卡片中的圖片URL
                                    "size": "full",
                                    "aspectMode": "cover",
                                    "aspectRatio": "1:1"
                                },
                                {
                                    "type": "text",
                                    "text": card['word'],
                                    "size": "xxs",
                                    "align": "center",
                                    "wrap": True,
                                    "maxLines": 2
                                }
                            ]
                        }
                    else:
                        # 音頻卡 - 添加按鈕
                        card_box = {
                            "type": "box",
                            "layout": "vertical",
                            "width": "60px",
                            "height": "80px",
                            "backgroundColor": "#FFF4E6",
                            "cornerRadius": "4px",
                            "borderWidth": "1px",
                            "borderColor": "#AAAAAA",
                            "contents": [
                                {
                                    "type": "text",
                                    "text": "🎵",
                                    "size": "lg",
                                    "align": "center",
                                    "color": "#FF6B6E"
                                },
                                {
                                    "type": "text",
                                    "text": card['thai'],
                                    "size": "xxs",
                                    "align": "center",
                                    "wrap": True,
                                    "maxLines": 2
                                }
                            ],
                            "action": {
                                "type": "message",
                                "text": f"播放音頻:{card['word']}"
                            }
                        }
                        
                        # 移除自動播放音頻的代碼，防止重複播放
                        # 音頻播放已在 handle_memory_game 中處理
                else:
                    # 未翻開的卡片 - 保持原樣
                    card_box = {
                        "type": "box",
                        "layout": "vertical",
                        "width": "60px",
                        "height": "80px",
                        "backgroundColor": "#4A86E8",
                        "cornerRadius": "4px",
                        "borderWidth": "1px",
                        "borderColor": "#0B5ED7",
                        "contents": [
                            {
                                "type": "text",
                                "text": "🎴",
                                "color": "#FFFFFF",
                                "align": "center",
                                "gravity": "center",
                                "size": "xl"
                            },
                            {
                                "type": "text",
                                "text": f"{card_id}",
                                "color": "#FFFFFF",
                                "align": "center",
                                "size": "sm"
                            }
                        ],
                        "action": {
                            "type": "message",
                            "text": f"翻牌:{card_id}"
                        }
                    }

                card_contents.append(card_box)

            row_bubble = {
                "type": "bubble",
                "body": {
                    "type": "box",
                    "layout": "horizontal",
                    "contents": card_contents
                }
            }
            bubbles.append(row_bubble)

        # 限制 bubbles 數量
        bubbles = bubbles[:10]
        
        logger.info(f"創建 Flex Message，Bubble 數量: {len(bubbles)}")
        
        flex_message = {
            "type": "carousel",
            "contents": bubbles
        }
        
        return FlexSendMessage(alt_text="泰語記憶翻牌遊戲", contents=flex_message)

    except Exception as e:
        logger.error(f"創建 Flex Message 時發生錯誤: {str(e)}")
        return TextSendMessage(text="遊戲畫面出現異常，請稍後再試")

# === 文字訊息處理 ===
@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    text = event.message.text
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的文字訊息: {text}")
    
    # 更新用戶活躍狀態
    user_data_manager.update_streak(user_id)
    
    # 記憶遊戲相關指令
    if text == "開始記憶遊戲" or text.startswith("記憶遊戲主題:") or text.startswith("翻牌:") or text.startswith("已翻開:") or text.startswith("播放音頻:"):
        game_response = handle_memory_game(user_id, text)
        line_bot_api.reply_message(event.reply_token, game_response)
        return
    
    # 播放音頻請求
    if text.startswith("播放音頻:"):
        word = text[5:]  # 提取詞彙
        if word in thai_data['basic_words']:
            word_data = thai_data['basic_words'][word]
            if 'audio_url' in word_data and word_data['audio_url']:
                line_bot_api.reply_message(
                    event.reply_token,
                    AudioSendMessage(
                        original_content_url=word_data['audio_url'],
                        duration=3000  # 假設音訊長度為3秒
                    )
                )
                return
    
    # 主選單與基本導航
    if text == "開始學習" or text == "返回主選單":
        line_bot_api.reply_message(event.reply_token, show_main_menu())
    
    # 選擇主題
    elif text == "選擇主題":
        line_bot_api.reply_message(event.reply_token, show_category_menu())
    
    # 主題選擇處理
    elif text.startswith("主題:"):
        category = text[3:]  # 取出主題名稱
        # 轉換成英文鍵值
        category_map = {
            "日常用語": "daily_phrases",
            "數字": "numbers",
            "動物": "animals",
            "食物": "food",
            "交通工具": "transportation"
        }
        if category in category_map:
            eng_category = category_map[category]
            user_data['current_category'] = eng_category
            messages = start_image_learning(user_id, eng_category)
            line_bot_api.reply_message(event.reply_token, messages)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="抱歉，無法識別該主題。請重新選擇。")
            )
    
    # 學習模式選擇
    elif text == "詞彙學習":
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "練習發音":
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "音調學習":
        messages = start_tone_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    # 進度與導航控制
    elif text == "下一個詞彙":
        # 如果有當前主題，在同一主題中選擇新詞彙
        if user_data.get('current_category'):
            category = user_data['current_category']
            user_data['current_vocab'] = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則清除當前詞彙，隨機選擇
            user_data['current_vocab'] = None
        
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "學習進度":
        progress_message = show_learning_progress(user_id)
        line_bot_api.reply_message(event.reply_token, progress_message)
    
    elif text == "練習弱點":
        # 找出評分最低的詞彙進行練習
        if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="您還沒有足夠的學習記錄，請先進行一些詞彙學習和發音練習！")
            )
            return
            
        # 找出分數最低的詞彙
        worst_word = min(user_data['vocab_mastery'].items(), 
                      key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
        
        # 設置為當前詞彙並啟動練習
        user_data['current_vocab'] = worst_word[0]
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "學習日曆":
        # 顯示用戶的學習日曆和連續學習天數
        streak = user_data.get('streak', 0)
        last_active = user_data.get('last_active', '尚未開始學習')
        
        calendar_message = f"您的學習記錄：\n\n"
        calendar_message += f"連續學習天數：{streak} 天\n"
        calendar_message += f"最近學習日期：{last_active}\n\n"
        calendar_message += "繼續保持學習熱情！每天學習一點，泰語能力會穩步提高。"
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=calendar_message)
        )
    
    else:
        # 默認回覆
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請選擇「開始學習」或點擊選單按鈕開始泰語學習之旅")
        )
    # 主程序入口 (放在最後)
if __name__ == "__main__":
    # 啟動 Flask 應用，使用環境變數設定的端口或默認5000
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"應用啟動在端口 {port}")
    app.run(host='0.0.0.0', port=port)
    
    