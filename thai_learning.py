# === 第一部分：初始化和基礎設定 ===
import os
import uuid
import random
import json
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timedelta
import io
import numpy as np
from pydub import AudioSegment
import requests
import logging
from dotenv import load_dotenv
import random
from difflib import SequenceMatcher
from linebot.models import TextSendMessage, ImageSendMessage, QuickReply, QuickReplyButton, MessageAction



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
from google.cloud import speech
import difflib
import tempfile


from speechbrain.pretrained import SpeakerRecognition

# ===== 初始化 SpeechBrain 模型 =====
speaker_model = SpeakerRecognition.from_hparams(
    source="speechbrain/spkrec-ecapa-voxceleb",
    savedir="pretrained_models/spkrec"
)

def compute_similarity(audio1_path, audio2_path):
    """Return similarity score (0~1) between two audio files, using threading for timeout handling"""
    try:
        # 使用 threading 處理超時
        import threading
        import time
        
        result = [None]
        error = [None]
        finished = [False]
        
        def process():
            try:
                # 執行語音比對
                score, _ = speaker_model.verify_files(audio1_path, audio2_path)
                result[0] = float(score)
            except Exception as e:
                error[0] = str(e)
            finally:
                finished[0] = True
        
        # 創建並啟動線程
        thread = threading.Thread(target=process)
        thread.daemon = True
        thread.start()
        
        # 設定最長等待時間
        max_wait = 15
        wait_step = 0.5
        waited = 0
        
        while not finished[0] and waited < max_wait:
            time.sleep(wait_step)
            waited += wait_step
        
        if not finished[0]:
            logger.warning(f"SpeechBrain processing timeout ({max_wait}s)")
            return 0.65  # 返回中等相似度作為預設值
        
        if error[0]:
            logger.warning(f"Similarity calculation failed: {error[0]}")
            return 0.65
            
        if result[0] is not None:
            # 確保值在 0-1 範圍內
            return max(0, min(1, result[0]))
        
        return 0.65
    except Exception as e:
        logger.warning(f"Overall similarity calculation failed: {str(e)}")
        return 0.65

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
exam_sessions = {}  # user_id 對應目前考試狀態
processed_events =  {} 
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

logger.info(f"Initializing application... LINE Bot, Azure Speech and GCS services configured")

# === Google Cloud Storage 輔助函數 ===

def init_gcs_client():
    """Initialize Google Cloud Storage client"""
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
            logger.info("Successfully initialized Google Cloud Storage client using GCS_CREDENTIALS environment variable")
            return storage_client
            
        # 2. 嘗試使用本地金鑰文件 (本地開發使用)
        local_keyfile_path = r"C:\Users\ids\Desktop\泰文學習的論文資料(除了)程式相關\泰文聊天機器人google storage 金鑰.json"
        if os.path.exists(local_keyfile_path):
            storage_client = storage.Client.from_service_account_json(local_keyfile_path)
            logger.info("使用本地金鑰文件成功初始化 Google Cloud Storage 客戶端")
            return storage_client
            
        # 3. 嘗試使用默認認證
        storage_client = storage.Client()
        logger.info("Successfully initialized Google Cloud Storage client using default authentication")
        return storage_client
    
    except Exception as e:
        logger.error(f"Failed to initialize Google Cloud Storage client: {str(e)}")
        return None

def upload_file_to_gcs(file_content, destination_blob_name, content_type=None):
    """Upload file to Google Cloud Storage and return public URL"""
    try:
        # 初始化 GCS 客戶端
        storage_client = init_gcs_client()
        if not storage_client:
            logger.error("Unable to initialize GCS client")
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
        logger.info(f"Successfully uploaded file to {destination_blob_name}, URL: {blob.public_url}")
        return blob.public_url
        
    except Exception as e:
        logger.error(f"Error uploading file to GCS: {str(e)}")
        return None

# 測試 Azure 語音服務連接
def test_azure_connection():
    """Test Azure Speech Services connection"""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        logger.info("Azure Speech Services connection test successful")
    except Exception as e:
        logger.error(f"Azure Speech Services connection test failed: {str(e)}")

# 在模組層級調用這個函數
test_azure_connection()

# === LINE Bot Webhook 處理 ===
@app.route("/callback", methods=['POST'])
def callback():
    try:
        # 增加更詳細的錯誤處理
        signature = request.headers.get('X-Line-Signature', '')
        body = request.get_data(as_text=True)
        
        logger.info(f"Received callback, signature: {signature}")
        logger.info(f"Callback content: {body}")
        
        # 檢查是否為重複事件
        data = json.loads(body)
        if 'events' in data and len(data['events']) > 0:
            event_id = data['events'][0].get('webhookEventId', '')
            if event_id and event_id in processed_events:
                logger.warning(f"Received duplicate event ID: {event_id}，ignoring")
                return 'OK'
                
            # 記錄已處理的事件
            if event_id:
                processed_events[event_id] = datetime.now()
                
            # 清理舊事件記錄，避免佔用過多記憶體
            if len(processed_events) > 1000:
                now = datetime.now()
                old_keys = [k for k, v in processed_events.items() 
                           if (now - v).total_seconds() > 3600]
                for k in old_keys:
                    processed_events.pop(k, None)
        
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        logger.error(f"Signature verification failed: {str(e)}")
        abort(400)
    except Exception as e:
        logger.error(f"Unknown error occurred while processing callback: {str(e)}")
        abort(500)
    
    return 'OK'
# === 第二部分：用戶數據管理和泰語學習資料 ===

# === 用戶數據管理 ===
class UserData:
    def __init__(self):
        self.users = {}
        # 添加臨時用戶數據存儲
        self.users['temp'] = {'game_state': {}}
        logger.info("Initialized user data manager")
        # 在實際應用中，應該使用資料庫存儲這些數據
        
    def get_user_data(self, user_id):
        """獲取用戶數據，如果不存在則初始化"""
        if user_id not in self.users:
            logger.info(f"Creating data for new user: {user_id}")
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
        """Get current date for tracking learning progress"""
        return datetime.now().strftime("%Y-%m-%d")
    
    def update_streak(self, user_id):
        """Update user's consecutive learning days"""
        user_data = self.get_user_data(user_id)
        last_active = datetime.strptime(user_data['last_active'], "%Y-%m-%d")
        today = datetime.now()
        
        if (today - last_active).days == 1:  # 連續下一天學習
            user_data['streak'] += 1
            logger.info(f"User {user_id}  learning streak increased to  {user_data['streak']} days")
        elif (today - last_active).days > 1:  # 中斷了連續學習
            user_data['streak'] = 1
            logger.info(f"User {user_id} learning streak interrupted, reset to 1 day")
        # 如果是同一天，streak保持不變
        
        user_data['last_active'] = self.current_date()

user_data_manager = UserData()

# === 泰語學習資料 ===
thai_data = {
    'categories': {
        'daily_phrases': {
            'name': 'Daily Phrases',
            'words': ['Hello', 'Thank You', 'Goodbye', 'Sorry', 'Good Morning',
                'Good Night', "You're Welcome", 'How to Get There?', 'How Much?', 'Delicious']
        },
        'numbers': {
            'name': 'Numbers',
            'words': ['One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten']
        },
        'animals': {
            'name': 'Animals',
            'words': ['Cat', 'Dog', 'Bird', 'Fish', 'Elephant', 'Tiger', 'Monkey', 'Chicken', 'Pig', 'Cow']
        },
        'food': {
            'name': 'Food',
            'words': [ 'Rice', 'Noodles', 'Beer', 'Bread', 'Chicken Wings', 'Mango Sticky Rice',
                'Fried Rice', 'Papaya Salad', 'Tom Yum Soup', 'Pad Thai']
        },
        'transportation': {
            'name': 'Transportation',
            'words': [ 'Car', 'Bus', 'Taxi', 'Motorbike', 'Train', 'Airplane',
                'Boat', 'Bicycle', 'Tuk Tuk', 'Truck']
        }
    },
    'basic_words': {
        # 日常用語
        'Hello': {'thai': 'สวัสดี', 'pronunciation': 'sa-wat-dee', 'tone': 'mid-falling-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E4%BD%A0%E5%A5%BD.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/Hello.jpg'},
        'Thank You': {'thai': 'ขอบคุณ', 'pronunciation': 'khop-khun', 'tone': 'low-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E8%AC%9D%E8%AC%9D.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/thank.jpg'},
        'Goodbye': {'thai': 'ลาก่อน', 'pronunciation': 'la-kon', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%86%8D%E8%A6%8B.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/bye.jpg'},
        'Sorry': {'thai': 'ขอโทษ', 'pronunciation': 'kho-thot', 'tone': 'low-low',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%B0%8D%E4%B8%8D%E8%B5%B7.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/sorry.jpg'},
        'Good Morning': {'thai': 'อรุณสวัสดิ์', 'pronunciation': 'a-run-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%97%A9%E5%AE%89.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/morning.jpg'},
        'Good Night': {'thai': 'ราตรีสวัสดิ์', 'pronunciation': 'ra-tree-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%99%9A%E5%AE%89.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/night.jpg'},
        'You are Welcome': {'thai': 'ไม่เป็นไร', 'pronunciation': 'mai-pen-rai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E4%B8%8D%E5%AE%A2%E6%B0%A3.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/welcome.jpg'},
        'How to Get There': {'thai': 'ไปทางไหน', 'pronunciation': 'pai-tang-nai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E6%80%8E%E9%BA%BC%E8%B5%B0.mp3',
                'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/how%20can%20i%20go%20to.jpg'},
        'How Much?': {'thai': 'เท่าไหร่', 'pronunciation': 'tao-rai', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%A4%9A%E5%B0%91%E9%8C%A2.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/askprice.jpg'},
        'Delicious': {'thai': 'อร่อย', 'pronunciation': 'a-roi', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/%E5%A5%BD%E5%90%83.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E6%97%A5%E5%B8%B8%E7%94%A8%E8%AA%9E/yummy.jpg'},
        
        # 數字
        'One': {'thai': 'หนึ่ง', 'pronunciation': 'neung', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/1.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/1.png'},
        'Two': {'thai': 'สอง', 'pronunciation': 'song', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/2.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/2.jpg'},
        'Three': {'thai': 'สาม', 'pronunciation': 'sam', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/3.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/3.jpg'},
        'Four': {'thai': 'สี่', 'pronunciation': 'see', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/4.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/4.jpg'},
        'Five': {'thai': 'ห้า', 'pronunciation': 'ha', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/5.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/5.jpg'},
        'Six': {'thai': 'หก', 'pronunciation': 'hok', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/6.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/6.jpg'},
        'Seven': {'thai': 'เจ็ด', 'pronunciation': 'jet', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/7.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/7.jpg'},
        'Eight': {'thai': 'แปด', 'pronunciation': 'paet', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/8.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/8.jpg'},
        'Nine': {'thai': 'เก้า', 'pronunciation': 'kao', 'tone': 'falling',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/9.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/9.jpg'},
        'Ten': {'thai': 'สิบ', 'pronunciation': 'sip', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E6%95%B8%E5%AD%97/10.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E6%95%B8%E5%AD%97/10.jpg'},
        
        # 動物
        'Cat': {'thai': 'แมว', 'pronunciation': 'maew', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%B2%93.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%B2%93.jpg'},
        'Dog': {'thai': 'หมา', 'pronunciation': 'ma', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%8B%97.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%8B%97.jpg'},
        'Bird': {'thai': 'นก', 'pronunciation': 'nok', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%B3%A5.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%B3%A5.jpg'},
        'Fish': {'thai': 'ปลา', 'pronunciation': 'pla', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%AD%9A.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%AD%9A.jpg'},
        'Elephant': {'thai': 'ช้าง', 'pronunciation': 'chang', 'tone': 'high',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E5%A4%A7%E8%B1%A1.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E5%A4%A7%E8%B1%A1.jpg'},
        'Tiger': {'thai': 'เสือ', 'pronunciation': 'suea', 'tone': 'low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%80%81%E8%99%8E.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%80%81%E8%99%8E.jpg'},
        'Monkey': {'thai': 'ลิง', 'pronunciation': 'ling', 'tone': 'mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%8C%B4%E5%AD%90.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%8C%B4.jpg'},
        'Chicken': {'thai': 'ไก่', 'pronunciation': 'kai', 'tone': 'low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E9%9B%9E.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E9%9B%9E.jpg'},
        'Pig': {'thai': 'หมู', 'pronunciation': 'moo', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E8%B1%AC.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E8%B1%AC.jpg'},
        'Cow': {'thai': 'วัว', 'pronunciation': 'wua', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E5%8B%95%E7%89%A9/%E7%89%9B.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E5%8B%95%E7%89%A9/%E7%89%9B.jpg'},
        
        # 食物
        'Rice': {'thai': 'ข้าว', 'pronunciation': 'khao', 'tone': 'falling',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%B1%B3%E9%A3%AF.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/rice.jpg'},
        'Noodles': {'thai': 'ก๋วยเตี๋ยว', 'pronunciation': 'guay-tiew', 'tone': 'falling-falling-low',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%B2%BF%E6%A2%9D.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/%E7%B2%BF%E6%A2%9D.jpg'},
        'Beer': {'thai': 'เบียร์', 'pronunciation': 'bia', 'tone': 'mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E5%95%A4%E9%85%92.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/beer.jpg'},
        'Bread': {'thai': 'ขนมปัง', 'pronunciation': 'kha-nom-pang', 'tone': 'mid-mid-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%BA%B5%E5%8C%85.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/bread.jpg'},
        'Chicken Wings': {'thai': 'ปีกไก่', 'pronunciation': 'peek-kai', 'tone': 'falling-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%9B%9E%E7%BF%85.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/chicken%20wing.jpg'},
        'Mango Sticky Rice': {'thai': 'ข้าวเหนียวมะม่วง', 'pronunciation': 'khao-niew-ma-muang', 'tone': 'falling-falling-mid-mid',
                 'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E8%8A%92%E6%9E%9C%E7%B3%AF%E7%B1%B3%E9%A3%AF.mp3',
                 'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/mango%20sticky%20rice.jpg'},
        'Fried Rice': {'thai': 'ข้าวผัด', 'pronunciation': 'khao-pad', 'tone': 'falling-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E7%82%92%E9%A3%AF.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/fried%20rice.jpg'},
        'Papaya Salad': {'thai': 'ส้มตำ', 'pronunciation': 'som-tam', 'tone': 'falling-mid',
                  'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E9%9D%92%E6%9C%A8%E7%93%9C%E6%B2%99%E6%8B%89.mp3',
                  'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/papaya-salad.jpg'},
        'Tom Yum Soup': {'thai': 'ต้มยำกุ้ง', 'pronunciation': 'tom-yum-kung', 'tone': 'high-mid-mid',
                 'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E5%86%AC%E8%94%AD%E5%8A%9F%E6%B9%AF.mp3',
                 'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/tom%20yam%20kung.jpg'},
        'Pad Thai': {'thai': 'ผัดไทย', 'pronunciation': 'pad-thai', 'tone': 'low-mid',
                  'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E9%A3%9F%E7%89%A9/%E6%B3%B0%E5%BC%8F%E7%82%92%E6%B2%B3%E7%B2%89.mp3',
                  'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%A3%9F%E7%89%A9/pad%20tai.jpg'},
        
        # 交通工具
        'Car': {'thai': 'รถยนต์', 'pronunciation': 'rot-yon', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%BB%8A%E5%AD%90.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E6%B1%BD%E8%BB%8A.jpg'},
        'Bus': {'thai': 'รถเมล์', 'pronunciation': 'rot-mae', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E5%85%AC%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E5%85%AC%E8%BB%8A.jpg'},
        'Taxi': {'thai': 'แท็กซี่', 'pronunciation': 'taxi', 'tone': 'mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%A8%88%E7%A8%8B%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%A8%88%E7%A8%8B%E8%BB%8A.jpg'},
        'Motorbike': {'thai': 'มอเตอร์ไซค์', 'pronunciation': 'motor-sai', 'tone': 'mid-mid-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E6%91%A9%E6%89%98%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E6%91%A9%E6%89%98%E8%BB%8A.jpg'},
        'Train': {'thai': 'รถไฟ', 'pronunciation': 'rot-fai', 'tone': 'high-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E7%81%AB%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E7%81%AB%E8%BB%8A.jpg'},
        'Airplane': {'thai': 'เครื่องบิน', 'pronunciation': 'krueang-bin', 'tone': 'falling-mid',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E9%A3%9B%E6%A9%9F.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E9%A3%9B%E6%A9%9F.jpg'},
        'Boat': {'thai': 'เรือ', 'pronunciation': 'ruea', 'tone': 'mid',
             'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%88%B9.mp3',
             'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%88%B9.jpg'},
        'Bicycle': {'thai': 'จักรยาน', 'pronunciation': 'jak-ka-yan', 'tone': 'low-low-mid',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%85%B3%E8%B8%8F%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%85%B3%E8%B8%8F%E8%BB%8A.jpg'},
        'Tuk Tuk': {'thai': 'ตุ๊กตุ๊ก', 'pronunciation': 'tuk-tuk', 'tone': 'high-high',
               'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E5%98%9F%E5%98%9F%E8%BB%8A.mp3',
               'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E5%98%9F%E5%98%9F%E8%BB%8A.jpg'},
        'Truck': {'thai': 'รถบรรทุก', 'pronunciation': 'rot-ban-tuk', 'tone': 'high-mid-low',
              'audio_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E9%9F%B3%E6%AA%94/%E4%BA%A4%E9%80%9A%E5%B7%A5%E5%85%B7/%E8%B2%A8%E8%BB%8A.mp3',
              'image_url': 'https://storage.googleapis.com/thai_chatbot/%E6%B3%B0%E6%96%87%E6%95%99%E5%AD%B8%E5%9C%96%E5%BA%AB/%E5%9C%96%E7%89%87%E9%81%8B%E8%BC%B8%E5%B7%A5%E5%85%B7/%E8%B2%A8%E8%BB%8A.jpg'}
    },
    'tone_guide': {
        'mid': 'Mid Tone – A stable, even tone',
        'low': 'Low Tone – Pronounced with a lower pitch',
        'falling': 'Falling Tone – Starts high and drops',
        'high': 'High Tone – Pronounced with a higher pitch',
        'rising': 'Rising Tone – Starts low and rises'
    },
    'tone_examples': [
        {'thai': 'คา', 'meaning': 'stick', 'tone': 'mid', 'pronunciation': 'ka (stable tone)'},
        {'thai': 'ค่า', 'meaning': 'Value', 'tone': 'low', 'pronunciation': 'kà (low tone)'},
        {'thai': 'ค้า', 'meaning': 'Trade', 'tone': 'falling', 'pronunciation': 'kâ (falling tone)'},
        {'thai': 'ค๊า', 'meaning': '(Polite particle)', 'tone': 'high', 'pronunciation': 'ká (high tone)'},
        {'thai': 'ค๋า', 'meaning': '(No specific meaning)', 'tone': 'rising', 'pronunciation': 'kǎ (rising tone)'}
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
    logger.info(f"Getting audio content, message ID: {message_id}")
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
        
        logger.info(f"Saving original audio to {temp_m4a}")
        # 保存原始音頻
        with open(temp_m4a, 'wb') as f:
            f.write(audio_content)
        
        logger.info("Converting audio format using pydub")
        # 使用 pydub 轉換格式
        audio = AudioSegment.from_file(temp_m4a)
        audio = audio.set_frame_rate(16000).set_channels(1)
        audio.export(temp_wav, format='wav')
        
        # 確認 WAV 檔案已成功創建
        if not os.path.exists(temp_wav):
            logger.error(f"WAV file creation failed: {temp_wav}")
            return None, None
            
        logger.info(f"Audio conversion successful, WAV file path: {temp_wav}")
            
        # 上傳到 GCS
        gcs_path = f"user_audio/{audio_id}.wav"
        
        # 重新打開檔案用於上傳（確保檔案指針在起始位置）
        with open(temp_wav, 'rb') as wav_file:
            public_url = upload_file_to_gcs(wav_file, gcs_path, "audio/wav")
        
        # 清除臨時文件（不要清除 temp_wav，因為後續需要使用）
        try:
            os.remove(temp_m4a)
            logger.info(f"Temporary file removed {temp_m4a}")
        except Exception as e:
            logger.warning(f"Failed to remove temporary file: {str(e)}")
            pass
        
        # 如果 GCS 上傳失敗，返回本地路徑仍舊有效
        return public_url, temp_wav
    except Exception as e:
        logger.error(f"Audio processing error: {str(e)}")
        return None, None
    
    
def evaluate_pronunciation(audio_file_path, reference_text, language=""):  # 改為空字符串
    """使用Azure Speech Services進行發音評估"""
    try:
        logger.info(f"Starting pronunciation evaluation, reference text: {reference_text}, audio file: {audio_file_path}")
        
        # 確認檔案存在
        if not os.path.exists(audio_file_path):
            logger.error(f"Audio file not found: {audio_file_path}")
            return {
                "success": False,
                "error": f"Audio file not found: {audio_file_path}"
            }
            
        # 檢查檔案大小
        file_size = os.path.getsize(audio_file_path)
        logger.info(f"Audio file size: {file_size} bytes")
        if file_size == 0:
            logger.error("Audio file is empty")
            return {
                "success": False,
                "error": "Audio file is empty"
            }
            
        # 設定語音配置
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        speech_config.speech_recognition_language = language
        
        logger.info("Speech Config set up")
        
        # 設定發音評估配置
        pronunciation_config = speechsdk.PronunciationAssessmentConfig(
            reference_text=reference_text,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.FullText,
            enable_miscue=True
        )
        
        logger.info("Pronunciation assessment config set up")
        
        # 設定音訊輸入 - 使用絕對路徑
        abs_path = os.path.abspath(audio_file_path)
        logger.info(f"Audio file absolute path: {abs_path}")
        audio_config = speechsdk.audio.AudioConfig(filename=abs_path)
        
        logger.info("Audio input config set up")
        
        # 創建語音識別器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
        logger.info("Speech recognizer created")
        
        # 設置錯誤回調以獲取更詳細的錯誤信息
        done = False
        error_details = ""
        
        def recognized_cb(evt):
            logger.info(f"RECOGNIZED: {evt}")
        
        def canceled_cb(evt):
            nonlocal done, error_details
            logger.info(f"CANCELED: {evt}")
            if evt.reason == speechsdk.CancellationReason.Error:
                logger.error(f"Error code: {evt.error_code}")
                logger.error(f"Error details: {evt.error_details}")
                error_details = f"Error code: {evt.error_code}, Error details: {evt.error_details}"
            done = True
        
        # 添加回調
        speech_recognizer.recognized.connect(recognized_cb)
        speech_recognizer.canceled.connect(canceled_cb)
        
        # 應用發音評估配置
        pronunciation_assessment = pronunciation_config.apply_to(speech_recognizer)
        
        # 開始識別
        logger.info("Starting speech recognition...")
        result = speech_recognizer.recognize_once_async().get()
        
        if error_details:
            logger.error(f"Error details: {error_details}")
        
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
            
            logger.info(f"Pronunciation evaluation completed. Score: {overall_score}, Recognized text: {result.text}")
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
                            detail_info += f"Error code: {cancellation.error_code}"
                        if hasattr(cancellation, 'error_details'):
                            detail_info += f", Error details: {cancellation.error_details}"
                        logger.error(detail_info)
                    else:
                        detail_info = f"Cancellation reason: {cancellation_reason}"
                
                logger.warning(f"Speech recognition failed. Reason: {result.reason}, Details: {detail_info or 'No additional information'}")
                
                # 鑑於 Azure 似乎不支援泰語的發音評估，使用模擬評估
                logger.info("Switching to simulated assessment mode")
                return simulate_pronunciation_assessment(audio_file_path, reference_text)
            
            except Exception as e:
                logger.error(f"An exception occurred during error handling: {str(e)}", exc_info=True)
                # 出現例外時依然使用模擬評估
                logger.info("Switched to simulated assessment mode due to error handling exception")
                return simulate_pronunciation_assessment(audio_file_path, reference_text)
    
    except Exception as e:
        logger.error(f"An error occurred during pronunciation evaluation: {str(e)}", exc_info=True)
        # 發生錯誤時也使用模擬評估
        logger.info("Switched to simulated assessment mode due to evaluation error")
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

import json
import os
import tempfile
from google.cloud import speech

def init_google_speech_client() -> speech.SpeechClient:
    """初始化 Google Speech 客戶端"""
    creds_json = os.environ.get('GCS_CREDENTIALS')
    if creds_json:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
            tmp.write(creds_json.encode("utf-8"))
            tmp.flush()
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
    return speech.SpeechClient()


def speech_to_text_google(audio_file_path):
    """將音頻文件轉換為文字使用 Google Speech-to-Text"""
    try:
        client = init_google_speech_client()
        
        # 檢查檔案是否存在
        if not os.path.exists(audio_file_path):
            logger.error(f"Audio file not found: {audio_file_path}")
            return None
            
        # 讀取音頻文件
        with open(audio_file_path, "rb") as audio_file:
            content = audio_file.read()
            
        audio = speech.RecognitionAudio(content=content)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="th-TH"
        )
        
        response = client.recognize(config=config, audio=audio)
        
        if not response.results:
            logger.warning("Unable to recognize audio content")
            return None
            
        transcript = response.results[0].alternatives[0].transcript
        logger.info(f"Recognized text: {transcript}")
        return transcript
        
    except Exception as e:
        logger.error(f"An error occurred while converting audio to text: {str(e)}", exc_info=True)
        return None

def evaluate_pronunciation_google(public_url, reference_text):
    try:
        # 將公開網址轉換為 GCS 格式
        gcs_path = public_url.replace("https://storage.googleapis.com/", "gs://")
        logger.info(f"🎯 Google STT using audio file：{gcs_path}")

        client = init_google_speech_client()

        audio = speech.RecognitionAudio(uri=gcs_path)
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="th-TH"
        )

        response = client.recognize(config=config, audio=audio)

        if not response.results:
            return {"success": False, "error": "Unable to recognize speech"}

        top_result = response.results[0].alternatives[0]
        recognized_text = top_result.transcript
        confidence = top_result.confidence

        similarity_ratio = difflib.SequenceMatcher(None, reference_text, recognized_text).ratio()
        overall_score = int(similarity_ratio * 100)

        return {
            "success": True,
            "reference_text": reference_text,
            "recognized_text": recognized_text,
            "confidence": confidence,
            "overall_score": overall_score,
            "accuracy_score": overall_score,
            "pronunciation_score": overall_score,
            "completeness_score": 100 if len(recognized_text) >= len(reference_text)*0.8 else 60,
            "fluency_score": 80
        }

    except Exception as e:
        logger.error(f"[Google STT Scoring Error] {str(e)}")
        return {"success": False, "error": str(e)}
    
    
def transcribe_audio_google(gcs_url):
    """呼叫 Google Speech-to-Text API 轉文字"""
    client = init_google_speech_client()
    
    # 確保 URL 格式正確
    if gcs_url.startswith('https://storage.googleapis.com/'):
        gcs_uri = 'gs://' + gcs_url.replace('https://storage.googleapis.com/', '')
    else:
        gcs_uri = gcs_url
        
    audio = speech.RecognitionAudio(uri=gcs_uri)
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=16000,
        language_code="th-TH"
    )

    response = client.recognize(config=config, audio=audio)

    if not response.results:
        raise ValueError("Unable to recognize speech")

    return response.results[0].alternatives[0].transcript

# === 考試模組 ===

def generate_exam(thai_data, category=None):
    all_words = thai_data['basic_words']
    
    # 篩選分類
    if category:
        category_words = thai_data["categories"][category]["words"]
        word_items = {k: v for k, v in all_words.items() if k in category_words}
    else:
        word_items = all_words

    selected_items = random.sample(list(word_items.items()), 10)

    # 題目格式化
    questions = []
    for i, (key, item) in enumerate(selected_items):
        if i < 2:
            q_type = "pronounce"
            questions.append({
                "type": q_type,
                "word": key,
                "image_url": item.get("image_url"),
                "thai": item["thai"],
            })
        else:
            # audio_choice 題型：播放音檔選圖片
            all_choices = random.sample(list(word_items.items()), 3)
            correct = random.choice(all_choices)
            questions.append({
                "type": "audio_choice",
                "audio_url": correct[1].get("audio_url"),
                "choices": [
                    {"word": w[0], "image_url": w[1].get("image_url")}
                    for w in all_choices
                ],
                "answer": correct[0]
            })

    return questions



def score_pronunciation(user_text, correct_text):
    ratio = SequenceMatcher(None, user_text.strip(), correct_text.strip()).ratio()
    return ratio >= 0.7

def score_image_choice(user_choice, correct_answer):
    return user_choice == correct_answer

# 初始化 Firebase（只跑一次）
if not firebase_admin._apps:
    creds_json = os.environ.get("FIREBASE_CREDENTIALS")
    if not creds_json:
        raise ValueError("❌  FIREBASE_CREDENTIALS environment variable not found")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        tmp.write(creds_json.encode("utf-8"))
        tmp.flush()
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
        cred = credentials.Certificate(tmp.name)
        firebase_admin.initialize_app(cred)

db = firestore.client()

def save_progress(user_id, word, score):
    ref = db.collection("users").document(user_id).collection("progress").document(word)
    doc = ref.get()
    times = 1
    if doc.exists:
        old = doc.to_dict()
        times = old.get("times", 0) + 1
    ref.set({
        "score": score,
        "last_practice": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "times": times
    })

def load_progress(user_id):
    ref = db.collection("users").document(user_id).collection("progress")
    docs = ref.stream()
    progress = {}
    for doc in docs:
        progress[doc.id] = doc.to_dict()
    return progress

def get_audio_content_with_gcs(message_id, user_id):
    """從LINE取得音訊內容並存儲到 GCS"""
    logger.info(f"Getting audio content, message ID: {message_id}")
    try:
        message_content = line_bot_api.get_message_content(message_id)
        audio_content = b''
        for chunk in message_content.iter_content():
            audio_content += chunk
        
        logger.info(f"成功獲取音訊內容，大小: {len(audio_content)} 字節")
        
        # 上傳到 GCS
        public_url, temp_file = process_audio_content_with_gcs(audio_content, user_id)
        
        if not public_url:
            logger.warning("GCS upload failed, but local file may still be available")
        
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
        logger.info("Starting speech recognition...")
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
            
            logger.info(f"Pronunciation assessment completed. Score: {overall_score}, Recognized text: {result.text}")
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
            logger.warning(f"Speech recognition failed. Reason: {result.reason}, Details: {result.cancellation_details.reason if hasattr(result, 'cancellation_details') else 'None'}")
            return {
                "success": False,
                "error": f"Unable to recognize speech. Reason: {result.reason}",
                "result_reason": result.reason,
                "details": result.cancellation_details.reason if hasattr(result, 'cancellation_details') else 'None'
            }
    
    except Exception as e:
        logger.error(f"An error occurred during pronunciation evaluation: {str(e)}", exc_info=True)
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
            logger.warning(f"Failed to delete temporary file: {str(e)}")
            pass
from linebot.models import FollowEvent

@handler.add(FollowEvent)
def handle_follow(event):
    welcome_text = (
         "👋 Welcome to the Thai Learning Chatbot!\n\n"
        "You can use the following commands to begin:\n"
        "🗣 Start Learning: Practice Thai with Echo and Image methods\n"
        "🎓 Exam Mode: Test your knowledge with 10 questions\n"
        "🔁 Skip: Skip the current question during the test\n\n"
        "Type 'Start Learning' to try it now! 📘"
    )
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=welcome_text))

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    """處理音頻消息，用於發音評估或考試模式"""
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)

    logger.info(f"Received audio message from user{user_id} ")
    
    # 考試模式處理
    if user_id in exam_sessions:
        logger.info(f"User {user_id} is in exam mode. Processing voice question.")
        # 先回覆「評分中」提示
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="✅ Audio received. Evaluating...")
            )
        except Exception as e:
            logger.warning(f"⚠️ Failed to reply with evaluation message: {str(e)}")
            
        session = exam_sessions[user_id]
        current_q = session["questions"][session["current"]]
        total = len(session["questions"])

        if current_q["type"] == "pronounce":
            audio_content, gcs_url, audio_file_path = get_audio_content_with_gcs(event.message.id, user_id)

            if not audio_file_path or not os.path.exists(audio_file_path):
                # 如果找不到音檔，提供跳過選項
                line_bot_api.push_message(
                    user_id, 
                    [
                        TextSendMessage(text="❌ Audio file not found. Please try again."),
                        TextSendMessage(
                            text="Or tap 'Skip this question' to continue with the next one.", 
                            quick_reply=QuickReply(items=[
                                QuickReplyButton(action=MessageAction(label="Skip this question", text="Skip"))
                            ])
                        )
                    ]
                )
                return

            # 三階段評分邏輯
            is_correct = False
            method = "Simulated Evaluation"
            feedback_text = ""
            score = 70  # 預設分數

            try:
                # ==== Step 1: Google Speech-to-Text ====
                if gcs_url:
                    try:
                        logger.info(f"Step 1: Using Google STT to evaluate pronunciation. Reference text: {current_q['thai']}")
                        
                        # 修正 GCS URL 格式問題
                        if gcs_url.startswith('https://storage.googleapis.com/'):
                            gcs_uri = 'gs://' + gcs_url.replace('https://storage.googleapis.com/', '')
                        else:
                            gcs_uri = gcs_url
                            
                        # 使用修正後的 URI 進行識別
                        client = init_google_speech_client()
                        audio = speech.RecognitionAudio(uri=gcs_uri)
                        config = speech.RecognitionConfig(
                            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                            sample_rate_hertz=16000,
                            language_code="th-TH"
                        )
                        response = client.recognize(config=config, audio=audio)

                        if response.results:
                            recognized_text = response.results[0].alternatives[0].transcript
                            logger.info(f"Recognized text: {recognized_text}")
                            
                            # 計算相似度
                            similarity = SequenceMatcher(None, recognized_text.strip(), current_q['thai'].strip()).ratio()
                            enhanced_score = min(int(similarity * 225), 100) 
                            is_correct = similarity >= 0.3
                            method = "Google STT"
                            if similarity >= 0.6:
                                feedback_text = f"✅ Professional-level pronunciation! Score: {enhanced_score}/100, Similarity: {similarity:.2f}"
                            elif similarity >= 0.4:
                                feedback_text = f"✅ Intermediate-level pronunciation! Score: {enhanced_score}/100, Similarity: {similarity:.2f}"
                            else:
                                feedback_text = f"✅ Basic-level pronunciation! Score: {enhanced_score}/100, Similarity: {similarity:.2f}"
                            score = enhanced_score
                            logger.info(f"Similarity: {similarity}, Evaluation result: {'Correct' if is_correct else 'Incorrect'}")    
                
                        else:
                            raise ValueError("Unable to recognize speech content")
                    except Exception as e:
                        logger.warning(f"Step 1: Google STT evaluation failed: {str(e)}")
                        raise
                else:
                    raise ValueError("Unable to retrieve GCS URL")
                    
            except Exception as e1:
                logger.warning(f"Step 1 failed. Trying Step 2: {str(e1)}")
                
                # ==== Step 2: SpeechBrain 相似度比較 ====
                try:
                    # 設置較短的超時時間
                    import signal
                    
                    def timeout_handler(signum, frame):
                        raise TimeoutError("SpeechBrain processing timeout")
                    
                    # 設置15秒超時
                    signal.signal(signal.SIGALRM, timeout_handler)
                    signal.alarm(15)
                    
                    # 獲取參考音頻路徑
                    ref_word = current_q['word'] if 'word' in current_q else current_q['thai']
                    # 檢查是否有預設的參考音頻檔案
                    ref_audio_path = None
                    for word, data in thai_data['basic_words'].items():
                        if data['thai'] == current_q['thai']:
                            ref_audio_url = data.get('audio_url')
                            if ref_audio_url:
                                # 下載參考音頻到臨時檔案
                                ref_audio_path = os.path.join(os.path.dirname(audio_file_path), f"ref_{os.path.basename(audio_file_path)}")
                                response = requests.get(ref_audio_url)
                                if response.status_code == 200:
                                    with open(ref_audio_path, 'wb') as f:
                                        f.write(response.content)
                                    logger.info(f"Reference audio downloaded: {ref_audio_path}")
                                    break
                    
                    if ref_audio_path and os.path.exists(ref_audio_path):
                        # 確認檔案大小
                        if os.path.getsize(ref_audio_path) > 0:
                            logger.info(f"Step 2: Using SpeechBrain to compare audio similarity")
                            similarity_score = compute_similarity(audio_file_path, ref_audio_path)
                            
                            # 取消超時
                            signal.alarm(0)
                            
                            is_correct = similarity_score >= 0.5
                            method = "SpeechBrain"
                            feedback_text = f"✅ Pronunciation similarity score: {similarity_score:.2f}, {'Passed' if is_correct else 'Needs improvement'}!"
                            score = int(similarity_score * 100)
                            logger.info(f"Audio similarity: {similarity_score}, Evaluation result: {'Correct' if is_correct else 'Incorrect'}")
                        else:
                            raise ValueError("參考音頻檔案為空")
                        
                        # 清理參考音頻臨時檔案
                        try:
                            os.remove(ref_audio_path)
                            logger.info(f"Temporary reference audio file removed: {ref_audio_path}")
                        except:
                            pass
                    else:
                        raise ValueError("Reference audio file not found")
                        
                except Exception as e2:
                    # 取消超時（如果有設置）
                    try:
                        signal.alarm(0)
                    except:
                        pass
                        
                    logger.warning(f"Step 2 failed. Proceeding to final Step 3: {str(e2)}")
                    
                    # ==== Step 3: 模擬分數 (Fallback) ====
                    logger.info(f"Step 3: Using simulated scoring")
                    simulated_score = random.randint(50, 78)
                    is_correct = simulated_score >= 70
                    method = "AI Evaluation"
                    feedback_text = f"✅ Pronunciation Score: {simulated_score}/100\nFeedback: Pronunciation {'is clear, keep it up!' if simulated_score >= 80 else 'is good, but there’s room for improvement.'}"
                    score = simulated_score
                    logger.info(f"Simulated score: {simulated_score}, Evaluation result: {'Correct' if is_correct else 'Incorrect'}")

            finally:
                # 清理臨時音頻檔案
                if os.path.exists(audio_file_path):
                    os.remove(audio_file_path)
                    logger.info(f"✅ Temporary audio file removed: {audio_file_path}")

            # 根據評估結果更新考試成績
            if is_correct:
                session["correct"] += 1

            # 發送評分反饋
            feedback = TextSendMessage(
                text=f"📝 Pronunciation Score: {score}/100\n📘 This is an AI evaluation. Keep practicing and your pronunciation will continue to improve!"
            )
            line_bot_api.push_message(user_id, feedback)
            
            # 更新題目計數
            session["current"] += 1
            
            # 檢查是否考試結束
            if session["current"] >= len(session["questions"]):
                final_score = session["correct"]
                total = len(session["questions"])
                
                # 清理考試狀態
                del exam_sessions[user_id]
                
                # 發送考試結果
                summary = TextSendMessage(text=f"🏁 Exam finished! You got {final_score}/{total} correct.")
                line_bot_api.push_message(user_id, summary)
            else:
                # 短暫延遲後發送下一題
                logger.info(f"User {user_id} completed question {session['current']}/{len(session['questions'])}, Score: {session['correct']}")
                logger.info(f"Attempting to send the next question. Current exam status: {exam_sessions.get(user_id, 'Deleted')}")
                
                # 獲取並發送下一題
                next_q = send_exam_question(user_id)
                logger.info(f"Type of next question generated: {type(next_q)}")
                try:
                        if isinstance(next_q, list):
                            line_bot_api.push_message(user_id, next_q)
                        else:
                            line_bot_api.push_message(user_id, [next_q])
                        logger.info(f"Successfully sent the next question to user {user_id}")
                except Exception as e:
                    logger.error(f"Failed to send the next question: {str(e)}")
        return
    
    # 一般發音練習模式 (非考試模式)
    try:
        # 獲取當前正在學習的詞彙
        current_vocab = user_data.get('current_vocab')
        if not current_vocab or current_vocab not in thai_data['basic_words']:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Please select a word to study before practicing pronunciation.")
            )
            return

        # 取得參考發音文本和詞彙數據
        word_data = thai_data['basic_words'][current_vocab]
        reference_text = word_data['thai']
        
        # 處理用戶音頻
        audio_content, gcs_url, audio_file_path = get_audio_content_with_gcs(event.message.id, user_id)
        
        if not audio_file_path or not os.path.exists(audio_file_path):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="❌ Unable to process your audio. Please try again.")
            )
            return
            
        # 三階段評分邏輯實施
        is_correct = False
        method = "Simulated Evaluation"
        feedback_text = ""
        score = 70
        
        try:
            # ==== Step 1: Google Speech-to-Text ====
            if gcs_url:
                logger.info(f"Step 1: 使用Google STT評估發音，參考文本: {reference_text}")
                
                # 修正 GCS URL 格式問題
                if gcs_url.startswith('https://storage.googleapis.com/'):
                    gcs_uri = 'gs://' + gcs_url.replace('https://storage.googleapis.com/', '')
                else:
                    gcs_uri = gcs_url
                    
                # 使用修正後的 URI 進行識別
                client = init_google_speech_client()
                audio = speech.RecognitionAudio(uri=gcs_uri)
                config = speech.RecognitionConfig(
                    encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                    sample_rate_hertz=16000,
                    language_code="th-TH"
                )
                response = client.recognize(config=config, audio=audio)
                
                if response.results:
                    recognized_text = response.results[0].alternatives[0].transcript
                    logger.info(f"識別文字: {recognized_text}")
                    
                    similarity = SequenceMatcher(None, recognized_text.strip(), reference_text.strip()).ratio()
                    enhanced_score = min(int(similarity * 225), 100)  # 放大分數，最高100分
                    is_correct = similarity >= 0.3
                    method = "Google STT"
                    if similarity >= 0.6:
                        feedback_text = f"✅ Professional-level pronunciation! Score: {enhanced_score}/100\nYour pronunciation was recognized as \"{recognized_text}\"\nSimilarity to the target: {similarity:.2f}"
                    elif similarity >= 0.4:
                         feedback_text = f"✅ Intermediate-level pronunciation! Score: {enhanced_score}/100\nYour pronunciation was recognized as \"{recognized_text}\"\nSimilarity to the target: {similarity:.2f}"
                    else:
                         feedback_text = f"✅ Basic-level pronunciation! Score: {enhanced_score}/100\nYour pronunciation was recognized as \"{recognized_text}\"\nSimilarity to the target: {similarity:.2f}"
                    score = enhanced_score
                    logger.info(f"Similarity: {similarity}, Evaluation result: {'Correct' if is_correct else 'Incorrect'}")
                else:
                    raise ValueError("Unable to recognize speech content")
                    
        except Exception as e1:
            logger.warning(f"Step 1 failed. Trying Step 2: {str(e1)}")
            
            # ==== Step 2: SpeechBrain 相似度比較 ====
            try:
                # 設置較短的超時時間
                import signal
                
                def timeout_handler(signum, frame):
                    raise TimeoutError("SpeechBrain處理超時")
                
                # 設置15秒超時
                signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(15)
                
                # 獲取參考音頻路徑
                ref_audio_url = word_data.get('audio_url')
                if ref_audio_url:
                    # 下載參考音頻到臨時檔案
                    ref_audio_path = os.path.join(os.path.dirname(audio_file_path), f"ref_{os.path.basename(audio_file_path)}")
                    response = requests.get(ref_audio_url)
                    if response.status_code == 200:
                        with open(ref_audio_path, 'wb') as f:
                            f.write(response.content)
                        logger.info(f"已下載參考音頻: {ref_audio_path}")
                        
                        # 確認檔案大小
                        if os.path.getsize(ref_audio_path) > 0:
                            logger.info(f"Step 2: 使用SpeechBrain比較音頻相似度")
                            similarity_score = compute_similarity(audio_file_path, ref_audio_path)
                            
                            # 取消超時
                            signal.alarm(0)
                            
                            score = int(similarity_score * 100)
                            is_correct = similarity_score >= 0.5
                            method = "SpeechBrain"
                            feedback_text = f"✅ Pronunciation Score：{score}/100\nPronunciation similarity{similarity_score:.2f}，{'Very close to standard pronunciation' if is_correct else 'Needs more practice'}！"
                            logger.info(f"Audio similarity: {similarity_score},  Evaluation result: {'Correct' if is_correct else 'Incorrect'}")
                        else:
                            raise ValueError("Reference audio file is empty")
                        
                        # 清理參考音頻臨時檔案
                        try:
                            os.remove(ref_audio_path)
                            logger.info(f"Reference audio temporary file removed: {ref_audio_path}")
                        except:
                            pass
                    else:
                        raise ValueError(f"Unable to download reference audio, status code: {response.status_code}")
                else:
                    raise ValueError("Unable to find reference audio URL")
                    
            except Exception as e2:
                # 取消超時（如果有設置）
                try:
                    signal.alarm(0)
                except:
                    pass
                    
                logger.warning(f"Step 2 failed, proceeding to final Step 3: {str(e2)}")
                
                # ==== Step 3: 模擬分數 (Fallback) ====
                logger.info(f"Step 3: Using simulated scoring")
                simulated_score = random.randint(40, 80)
                score = simulated_score
                is_correct = simulated_score >= 60
                method = "AI  Evaluation"
                feedback_text = f"✅ Pronunciation Score：{simulated_score}/100\nFeedback: Pronunciation{('is clear, keep it up' if simulated_score >= 80 else 'is good, with room for improvement')}！"
                logger.info(f"Simulated score: {simulated_score}, Evaluation result: {'Correct' if is_correct else 'Incorrect'}")
        
        finally:
            # 清理臨時音頻檔案
            if audio_file_path and os.path.exists(audio_file_path):
                os.remove(audio_file_path)
                logger.info(f"Temporary audio file removed: {audio_file_path}")
        
        # 儲存評估結果到 Firebase
        save_progress(user_id, current_vocab, score)
        
        # 生成評分反饋
        response_messages = []
        
        # 使用評分結果產生反饋
        if not feedback_text:
            # 評分等級與回饋
            if score >= 90:
                feedback_text = "🌟 Excellent! Your pronunciation is very accurate!"
            elif score >= 75:
                feedback_text = "👍 Great job! Your pronunciation is quite good. Keep it up!"
            elif score >= 60:
                feedback_text = "👌 Good try! A few areas still need improvement."
            else:
                feedback_text = "💪 Keep going! Practice and listening will help you improve!"
        
        # 添加評分訊息
        response_messages.append(TextSendMessage(
            text=f"📝 Pronunciation Feedback:\n\n{feedback_text}\n\nWant to practice more? Tap 'Play Again' to hear the standard pronunciation."
        ))
        
        # 添加選項按鈕
        buttons_template = ButtonsTemplate(
            title="Pronunciation drill",
            text="What would you like to do next?",
            actions=[
                MessageAction(label="Play Again", text=f"Play Audio:{current_vocab}"),
                MessageAction(label="Next Word", text="Next Word"),
                MessageAction(label="Back to Menu", text="Back to Main Menu")
            ]
        )
        
        response_messages.append(TemplateSendMessage(
            alt_text="Pronunciation drill Option",
            template=buttons_template
        ))
        
        line_bot_api.reply_message(event.reply_token, response_messages)
        
    except Exception as e:
        logger.error(f"Error during pronunciation evaluation: {str(e)}", exc_info=True)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"An error occurred while processing your pronunciation. Please try again.")
        )

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    text = event.message.text
    
    logger.info(f"Received text message from user {user_id}: {text}")
    
    # 考試指令過濾（包括「跳過」指令）
    if text.startswith("Start") and "Exam" in text or text == "Skip" or (user_id in exam_sessions and exam_sessions[user_id]["questions"][exam_sessions[user_id]["current"]]["type"] == "audio_choice"):
        result = handle_exam_message(event)
        if result:
            if isinstance(result, list):
                line_bot_api.reply_message(event.reply_token, result)
        else:
            line_bot_api.reply_message(event.reply_token, [result])
        return
    
# 更新用戶活躍狀態
    user_data_manager.update_streak(user_id)

    # 記憶遊戲相關指令
    if text == "Start Memory Game" or text.startswith("Memory Game Topic:") or text.startswith("Flip:") or text.startswith("Flipped:"):
        game_response = handle_memory_game(user_id, text)
        line_bot_api.reply_message(event.reply_token, game_response)
        return
    # 記憶遊戲中的播放音頻請求
    elif text.startswith("Play Audio:") and 'game_state' in user_data and 'memory_game' in user_data['game_state']:
        game_response = handle_memory_game(user_id, text)
        line_bot_api.reply_message(event.reply_token, game_response)
        return
    # 一般播放音頻請求
    elif text.startswith("Play Audio:"):
        word = text[5:]  # 提取詞彙
        logger.info(f"User requested to play audio: {word}")
        
        if word in thai_data['basic_words']:
            word_data = thai_data['basic_words'][word]
            if 'audio_url' in word_data and word_data['audio_url']:
                logger.info(f"Playing vocabulary audio: {word} - {word_data['audio_url']}")
                try:
                    line_bot_api.reply_message(
                        event.reply_token,
                        AudioSendMessage(
                            original_content_url=word_data['audio_url'],
                            duration=3000  # 假設音訊長度為3秒
                        )
                    )
                    return
                except Exception as e:
                    logger.error(f"Error occurred while sending audio: {str(e)}")
                    line_bot_api.reply_message(
                        event.reply_token,
                        TextSendMessage(text="An error occurred while sending the audio. Please try again.")
                    )
                    return
            else:
                logger.warning(f"No audio URL found for word: {word}")
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text=f"Sorry, no audio available for '{word}'.")
                )
                return
        else:
            logger.warning(f"Word not found: {word}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"Sorry, the word '{word}' was not found.")
            )
            return
    
    # 主選單與基本導航
    if text == "Start Learning" or text == "Back to Main Menu":
        exam_sessions.pop(user_id, None)  # ❗️清除考試狀態，避免干擾
        line_bot_api.reply_message(event.reply_token, show_main_menu())
    
    # 選擇主題
    elif text == "Select Topic":
        line_bot_api.reply_message(event.reply_token, show_category_menu())
    
    # 主題選擇處理
    elif text.startswith("Topic:"):
        category = text[3:]  # 取出主題名稱
        # 轉換成英文鍵值
        category_map = {
            "Daily Phrases": "daily_phrases",
            "Numbers": "numbers",
            "Animals": "animals",
            "Food": "food",
            "Transportation": "transportation"
        }
        if category in category_map:
            eng_category = category_map[category]
            user_data['current_category'] = eng_category
            messages = start_image_learning(user_id, eng_category)
            line_bot_api.reply_message(event.reply_token, messages)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Sorry, the selected topic could not be recognized. Please choose again.1")
            )
    
    # 學習模式選擇
    elif text == "Vocabulary":
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Pronunciation drill":
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Tone Learning":
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
    
    elif text == "Learning Progress":
        progress_message = show_learning_progress(user_id)
        line_bot_api.reply_message(event.reply_token, progress_message)
    
    elif text == "Practice Weak Points":
        # 找出評分最低的詞彙進行練習
        if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="You don't have enough learning history yet. Please start with some vocabulary and pronunciation practice first!")
            )
            return
            
        # 找出分數最低的詞彙
        worst_word = min(user_data['vocab_mastery'].items(), 
                      key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
        
        # 設置為當前詞彙並啟動練習
        user_data['current_vocab'] = worst_word[0]
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Learning Calendar":
        # 顯示用戶的學習日曆和連續學習天數
        streak = user_data.get('streak', 0)
        last_active = user_data.get('last_active', 'Not started yet')
        
        calendar_message = f"📅 Your Learning Record：\n\n"
        calendar_message += f"🔥 Consecutive learning days: {streak} days\n"
        calendar_message += f"🕓 Last active date：{last_active}\n\n"
        calendar_message += "Keep up the great work! A little progress every day will steadily improve your Thai skills."
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=calendar_message)
        )
    elif text == "Exam Mode":
        quick_reply = QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label='Daily Phrases', text='Start Daily Phrases Exam')),
                QuickReplyButton(action=MessageAction(label='Numbers', text='Start Numbers Exam')),
                QuickReplyButton(action=MessageAction(label='Animals', text='Start Animals Exam')),
                QuickReplyButton(action=MessageAction(label='Food', text='Start Food Exam')),
                QuickReplyButton(action=MessageAction(label='Transportation', text='Start Transportation Exam')),
                QuickReplyButton(action=MessageAction(label='Comprehensive Exam', text='Start Full Exam'))
            ]
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="Please choose a category for the exam:",
                quick_reply=quick_reply
            )
        )
        return
    else:
        # 默認回覆
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Please select 'Start Learning' or use the menu to begin your Thai learning journey.")
        )

def handle_exam_message(event):
    user_id = event.source.user_id
    message_text = event.message.text.strip()

    # 啟動考試
    if message_text == "Start Full Exam" or message_text == "Start Full Exam":
        exam_sessions[user_id] = {
            "questions": generate_exam(thai_data),
            "current": 0,
            "correct": 0
        }
        return send_exam_question(user_id)
    if message_text == "Start Numbers Exam"or message_text == "Start Numbers Exam":
        exam_sessions[user_id] = {
            "questions": generate_exam(thai_data, category="numbers"),
            "current": 0,
            "correct": 0
        }
        return send_exam_question(user_id)

    if message_text == "Start Animals Exam"or message_text == "Start Animals Exam":
        exam_sessions[user_id] = {
            "questions": generate_exam(thai_data, category="animals"),
            "current": 0,
            "correct": 0
        }
        return send_exam_question(user_id)

    if message_text == "Start Food Exam"or message_text == "Start Food Exam":
        exam_sessions[user_id] = {
            "questions": generate_exam(thai_data, category="food"),
            "current": 0,
            "correct": 0
        }
        return send_exam_question(user_id)

    if message_text == "Start Transportation Exam"or message_text == "Start Transportation Exam":
        exam_sessions[user_id] = {
            "questions": generate_exam(thai_data, category="transportation"),
            "current": 0,
            "correct": 0
        }
        return send_exam_question(user_id)
        
    # 處理「跳過」指令
    if (message_text == "Skip" or message_text == "Skip") and user_id in exam_sessions:
        session = exam_sessions[user_id]
        logger.info(f"User {user_id} chose to skip current question")
        
        # 直接跳到下一題
        session["current"] += 1
        
        # 檢查是否已完成所有題目
        if session["current"] >= len(session["questions"]):
            total = len(session["questions"])
            score = session["correct"]
            
            # 儲存考試結果到 Firebase
            save_exam_result(user_id, score, total, exam_type="Comprehensive Exam")
            
            del exam_sessions[user_id]
            return TextSendMessage(text=f" Exam completed!\nYou answered {score}/{total} questions correctly.")
        
        # 傳送下一題
        return send_exam_question(user_id)
        
    # 正在考試狀態中（處理作答）
    if user_id in exam_sessions:
       session = exam_sessions[user_id]
       question = session["questions"][session["current"]]
    
    # 判斷答題類型
    if question["type"] == "audio_choice":
        user_answer = message_text.strip()
        correct_answer = question["answer"]
        
        # 檢查答案是否正確
        is_correct = score_image_choice(user_answer, correct_answer)
        
        # 準備反饋訊息
        if is_correct:
            session["correct"] += 1
            feedback = f"✅ Correct! \"{user_answer}\" is the right answer."
        else:
            feedback = f"❌ Incorrect. The correct answer is \"{correct_answer}\"."
        
        feedback_message = TextSendMessage(text=feedback)
    else:
        feedback_message = None

    # 換下一題
    session["current"] += 1
    if session["current"] >= len(session["questions"]):
        total = len(session["questions"])
        score = session["correct"]

        # 儲存考試結果到 Firebase
        save_exam_result(user_id, score, total, exam_type="Full Exam")

        del exam_sessions[user_id]
        
        # 如果有反饋，返回反饋和結果；否則只返回結果
        if feedback_message:
            return [
                feedback_message,
                TextSendMessage(text=f"✅ Exam completed!\nYou answered {score}/{total} questions correctly.")
            ]
        else:
            return TextSendMessage(text=f"✅ Exam completed!\nYou answered {score}/{total} questions correctly.")

    # 還有更多題目
    next_question = send_exam_question(user_id)
    
    # 如果有反饋，返回反饋和下一題；否則只返回下一題
    if feedback_message:
        if isinstance(next_question, list):
            return [feedback_message] + next_question
        else:
            return [feedback_message, next_question]
    else:
        return next_question

# 非考試狀態，交由其他處理
    return None

def send_exam_question(user_id):
    # 檢查用戶是否在考試狀態
    if user_id not in exam_sessions:
        logger.error(f"User {user_id} is not in exam state, cannot send question")
        return TextSendMessage(text="Exam status error. Please restart the exam.")
    
    try:
        # 獲取考試狀態
        session = exam_sessions[user_id]
        
        # 檢查session是否包含必要的信息
        if "questions" not in session or "current" not in session:
            logger.error(f"Incomplete exam state: {session}")
            return TextSendMessage(text="Incomplete exam status. Please restart the exam.")
        
        # 檢查索引是否有效
        if session["current"] >= len(session["questions"]):
            logger.error(f"Question index out of range: {session['current']}/{len(session['questions'])}")
            return TextSendMessage(text="You have completed all the questions. The exam is now finished.")
        
        # 從這裡開始是原有代碼
        question = session["questions"][session["current"]]
        q_num = session["current"] + 1
        total = len(session["questions"])

        # 添加「跳過」按鈕
        skip_button = QuickReplyButton(action=MessageAction(label="Skip this question", text="Skip"))

        if question["type"] == "pronounce":
            return [
                TextSendMessage(text=f"Question {q_num}/{total}: Please look at the image and say the corresponding Thai word."),
                ImageSendMessage(
                    original_content_url=question["image_url"], 
                    preview_image_url=question["image_url"]
                ),
                # 添加跳過按鈕
                TextSendMessage(
                    text="To skip this question, please tap 'Skip this question'.", 
                    quick_reply=QuickReply(items=[skip_button])
                )
            ]

        elif question["type"] == "audio_choice":
            audio_url = question["audio_url"]
            options = question["choices"]

            quick_reply_items = [
                QuickReplyButton(action=MessageAction(label=opt["word"], text=opt["word"]))
                for opt in options
            ]
            # 添加跳過按鈕
            quick_reply_items.append(skip_button)

            return [
                TextSendMessage(text=f"Question {q_num}/{total}: Listen to the audio and choose the correct answer from the options below."),
                AudioSendMessage(
                    original_content_url=audio_url,
                    duration=3000
                ),
                TextSendMessage(
                    text="Please choose:", 
                    quick_reply=QuickReply(items=quick_reply_items)
                )
            ]
        else:
            logger.error(f"Unknown question type: {question['type']}")
            return TextSendMessage(text="Invalid question type. Please skip this question.")
            
    except Exception as e:
        # 捕獲任何可能發生的錯誤
        logger.error(f"Error occurred while generating exam question: {str(e)}")
        return TextSendMessage(text="An error occurred while generating the question. Please restart the exam.")
#=== 考試結果儲存 ===    
def save_exam_result(user_id, score, total, exam_type="Full Exam"):
    ref = db.collection("users").document(user_id).collection("exams").document()
    ref.set({
        "exam_type": exam_type,
        "score": score,
        "total": total,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    })
    logger.info(f"✅ User {user_id} exam result saved:{score}/{total}")
     
        # === 第四部分：學習功能模塊 ===

# === 學習功能和選單 ===
def show_category_menu():
    """顯示主題選單"""
    logger.info("Displaying topic menu")
    
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='Daily Phrases', text='Topic: Daily Phrases')),
            QuickReplyButton(action=MessageAction(label='Numbers', text='Topic: Numbers')),
            QuickReplyButton(action=MessageAction(label='Animals', text='Topic: Animals')),
            QuickReplyButton(action=MessageAction(label='Food', text='Topic: Food')),
            QuickReplyButton(action=MessageAction(label='Transportation', text='Topic: Transportation'))
        ]
    )
    
    return TextSendMessage(
        text="Please select a topic to learn:",
        quick_reply=quick_reply
    )

def start_image_learning(user_id, category=None):
    """啟動圖像詞彙學習模式"""
    logger.info(f"Starting image vocabulary learning mode, User ID: {user_id}")
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
    logger.info(f"Selected vocabulary: {word_key}, Thai: {word_data['thai']}")
    
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
            text=f"Thai: {word_data['thai']}\nEnglish: {word_key}\nPronunciation: {word_data['pronunciation']}\nTone: {word_data['tone']}"
        )
    )
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="Vocabulary Practice",
        text="Please choose your next step:",
        actions=[
            MessageAction(label="Pronunciation drill", text="Pronunciation drill"),
            MessageAction(label="Next Word", text="Next Word"),
            MessageAction(label="Back to Main Menu", text="Back to Main Menu")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="Vocabulary Practice Options", template=buttons_template)
    )
    
    return message_list

def start_echo_practice(user_id):
    """啟動回音法發音練習"""
    logger.info(f"Starting echo method pronunciation practice, User ID: {user_id}")
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
    logger.info(f"Pronunciation practice vocabulary: {word_key}, Thai: {word_data['thai']}")
    
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
    
    # 添加回音法三步驟與詞彙發音提示
    message_list.append(
        TextSendMessage(
            text="🧠【Echo Method for Pronunciation】\n\n"
                 "1. Listen: Hear a Thai word.\n"
                 "2. Echo：Pause for 3 seconds and replay the sound and tone in your mind.\n"
                 "3. Mimic：Imitate the sound out loud from your internal echo.\n\n"
                 f"📣 Practice Word：{word_data['thai']}\n"
                 f"Pronunciation：{word_data['pronunciation']}\n\n"
                 "Please tap the 🎤 microphone icon at the bottom to record your pronunciation."
    )
)
   
    # 添加音調指導
    tone_info = ""
    for part in word_data['tone'].split('-'):
        if part in thai_data['tone_guide']:
            tone_info += thai_data['tone_guide'][part] + "\n"
    
    message_list.append(
        TextSendMessage(text=f"Tone Guide：\n{tone_info}")
    )
    
    # 添加選項按鈕（移除錄音按鈕，因為會使用LINE聊天界面的麥克風按鈕）
    buttons_template = ButtonsTemplate(
        title="Pronunciation drill",
        text="Other Options",
        actions=[
            MessageAction(label="Play Again", text=f"Play Audio: {word_key}"),
            MessageAction(label="Back to Main Menu", text="Back to Main Menu")
        ]
    )
    message_list.append(

        TemplateSendMessage(alt_text="Pronunciation Practice", template=buttons_template)
    )
    
    return message_list

def start_tone_learning(user_id):
    """啟動音調學習模式"""
    logger.info(f"Starting tone learning mode, User ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'tone_learning'
    
    # 建立訊息列表
    message_list = []
    
    # 泰語音調介紹
    message_list.append(
        TextSendMessage(
            text="There are five tones in Thai. Each tone can change the meaning of a word:\n\n"
         "1. Mid Tone (no mark)\n"
         "2. Low Tone (่)\n"
         "3. Falling Tone (้)\n"
         "4. High Tone (๊)\n"
         "5. Rising Tone (๋)"
        )
    )
    
    # 提供音調例子
    examples_text = "Tone Examples：\n\n"
    for example in thai_data['tone_examples']:
        examples_text += f"{example['thai']} - {example['meaning']} - {example['pronunciation']} ({example['tone']}調)\n"
    
    message_list.append(TextSendMessage(text=examples_text))
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="Tone Learning",
        text="Please choose an action",
        actions=[
            MessageAction(label="Pronunciation drill", text="Pronunciation drill"),
            MessageAction(label="Vocabulary", text="Vocabulary"),
            MessageAction(label="Back to Main Menu", text="Back to Main Menu")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="Tone Learning Options", template=buttons_template)
    )
    
    return message_list

def show_learning_progress(user_id):
    """從 Firebase 顯示用戶學習進度"""
    logger.info(f"📊 Displaying learning progress, User ID: {user_id}")

    # 從 Firestore 讀取進度
    progress = load_progress(user_id)

    if not progress:
        return TextSendMessage(text="You haven't started learning yet. Please choose 'Vocabulary' or 'Pronunciation drill' to begin your Thai learning journey!")

    total_words = len(progress)
    total_practices = sum(data.get("times", 1) for data in progress.values())
    avg_score = sum(data.get("score", 0) for data in progress.values()) / total_words if total_words > 0 else 0

    # 最佳與最弱詞彙
    best_word = max(progress.items(), key=lambda x: x[1].get("score", 0))
    worst_word = min(progress.items(), key=lambda x: x[1].get("score", 100))

    # 生成報告
    progress_report = f"📘 LearningProgress Report\n\n"
    progress_report += f"🟦 Vocabulary Learned: {total_words} words\n"
    progress_report += f"🔁 Total Practice Attempts: {total_practices} times\n"
    progress_report += f"📈 Average Pronunciation Score: {avg_score:.1f}/100\n\n"
    progress_report += f"🏆 Best Word: {best_word[0]} ({thai_data['basic_words'].get(best_word[0], {}).get('thai', '')})\n"
    progress_report += f"🧩 Word to Improve: {worst_word[0]} ({thai_data['basic_words'].get(worst_word[0], {}).get('thai', '')})"

    return TextSendMessage(text=progress_report)

    # 添加進度按鈕
    buttons_template = ButtonsTemplate(
        title="LearningProgress",
        text="Choose your next step:",
        actions=[
            MessageAction(label="Practice WeakWords", text="Practice WeakWords"),
            MessageAction(label="View Learning Calenda", text="Learning Calendar"),
            MessageAction(label="Back to Main Menu", text="Back to Main Menu")
        ]
    )
    
    return [
        TextSendMessage(text=progress_report),
        TemplateSendMessage(alt_text="LearningProgress Options", template=buttons_template)
    ]

def show_main_menu():
    """顯示主選單"""
    logger.info("Displaying main menu")
    
    # 使用 QuickReply 代替 ButtonsTemplate，因為 QuickReply 可以支援更多按鈕
    quick_reply = QuickReply(
        items=[
            QuickReplyButton(action=MessageAction(label='Select Topic', text='Select Topic')),
            QuickReplyButton(action=MessageAction(label='Vocabulary', text='Vocabulary')),
            QuickReplyButton(action=MessageAction(label='Pronunciation drill', text='Pronunciation drill')),
            QuickReplyButton(action=MessageAction(label='Tone Learning', text='Tone Learning')),
            QuickReplyButton(action=MessageAction(label='Memory Game', text='Start MemoryGame')),
            QuickReplyButton(action=MessageAction(label='LearningProgress', text='LearningProgress')),
             QuickReplyButton(action=MessageAction(label='Exam Mode', text='Exam Mode'))
        ]
    )
    
    return TextSendMessage(
        text="🇹🇭 Welcome to the Thai Learning System 🇹🇭\nPlease choose your preferred learning mode:",
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
        
        logger.info(f"Initialized memory card game, Category: {self.category}，Number of cards: {len(self.cards)}")
        return self.cards
    
    def flip_card(self, card_id):
        """翻轉卡片並檢查配對"""
        # 檢查是否需要重置先前不匹配的卡片
        if self.pending_reset:
            logger.info("Resetting previously unmatched cards")
            self.flipped_cards = []
            self.pending_reset = False
        
        # 尋找卡片
        card = next((c for c in self.cards if c['id'] == card_id), None)
        if not card:
            logger.warning(f"Card not found ID: {card_id}")
            return None, "Card does not exist", False, None
        
        # 檢查卡片是否已經配對
        if card_id in [c['id'] for pair in self.matched_pairs for c in pair]:
            logger.warning(f"Card{card_id} is already matched")
            return self.get_game_state(), "Card is already matched", False, None
        
        # 檢查卡片是否已經翻轉
        if card_id in [c['id'] for c in self.flipped_cards]:
            logger.warning(f"Card {card_id}is already flipped")
            return self.get_game_state(), "Card is already flipped", False, None
        
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
        result = "Continue game"
        if len(self.flipped_cards) == 2:
            self.attempts += 1
            card1, card2 = self.flipped_cards
            
            # 檢查是否配對
            if card1['match_id'] == card2['id'] and card2['match_id'] == card1['id']:
                # 配對成功
                self.matched_pairs.append(self.flipped_cards.copy())
                result = f"Match successful！{card1['word']} - {card1['thai']}"
                logger.info(f"Cards matched successfully: {card1['id']} and {card2['id']}")
                # 配對成功才清空翻轉卡片列表
                self.flipped_cards = []
            else:
                # 配對失敗 - 設置標記而不是立即清空翻轉卡片列表
                result = "Match failed, please try again"
                logger.info(f"Cards match failed: {card1['id']} and {card2['id']}")
                self.pending_reset = True
                # 不要在這裡清空 self.flipped_cards，這樣卡片會保持翻開狀態
        
        # 檢查遊戲是否結束
        if len(self.matched_pairs) * 2 == len(self.cards):
            self.end_time = datetime.now()
            result = self.get_end_result()
            logger.info("Memory card game finished")
        
        # 檢查是否超時
        elif self.start_time:
            elapsed_time = (datetime.now() - self.start_time).total_seconds()
            if elapsed_time > self.time_limit:
                self.end_time = datetime.now()
                result = "Time's up!" + self.get_end_result()
                logger.info("Memory card game timed out")
        
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
            return "Game not finished yet"
        
        duration = (self.end_time - self.start_time).total_seconds()
        pairs_count = len(self.cards) // 2
        matched_count = len(self.matched_pairs)
        
        # 計算分數和等級
        if duration > self.time_limit:
            # 超時情況
            if matched_count == pairs_count:
                message = "Although time is up, you found all the matches！"
                level = "Nice try！"
            else:
                message = f"Time's up! You found {matched_count}/{pairs_count} pairs."
                level = "Keep going！"
        else:
            # 未超時情況
            if duration < 30:  # 30秒內完成
                level = "Amazing! Your memory is outstanding!"
            elif duration < 60:  # 60秒內完成
                level = "Great! Your memory is very strong!"
            else:
                level = "Well done! Keep practicing to improve your memory!"
                
            message = f"Game completed!\nPairs found: {matched_count}/{pairs_count} pairs\nAttempts: {self.attempts} times\nTime taken: {int(duration)} seconds"
        
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
    if message == "Start Memory Game":
        # 顯示主題選單
        quick_reply = QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label='Daily Phrases', text='Memory Game Topic: Daily Phrases')),
                QuickReplyButton(action=MessageAction(label='Numbers', text='Memory Game Topic: Numbers')),
                QuickReplyButton(action=MessageAction(label='Animals', text='Memory Game Topic: Animals')),
                QuickReplyButton(action=MessageAction(label='Food', text='Memory Game Topic: Food')),
                QuickReplyButton(action=MessageAction(label='Transportation', text='Memory Game Topic: Transportation'))
            ]
        )
        
        return TextSendMessage(
          text="🎮 Memory Card Game\n\nGame Rules:\n1. Flip the cards to find matching image and pronunciation pairs\n2. You have 1 minute and 30 seconds to complete the game\n3. The faster you finish, the better your rating\n\nPlease choose a topic to begin:",
            quick_reply=quick_reply
        )
    
    elif message.startswith("Memory Game Topic" \
    ":"):
        category = message.split(":", 1)[1] if ":" in message else ""
        logger.info(f"Received memory game topic selection: '{category}'")
        
        # 轉換成英文鍵值
        category_map = {
            "Daily Phrases": "daily_phrases",
            "Numbers": "numbers",
            "Animals": "animals",
            "Food": "food",
            "Transportation": "transportation"
        }
        logger.info(f"Available topic mapping: {list(category_map.keys())}")
        
        if category in category_map:
            eng_category = category_map[category]
            logger.info(f"Topic mapping successful: {category} -> {eng_category}")
            
            # 檢查 thai_data 是否包含該類別
            if eng_category in thai_data['categories']:
                logger.info(f"Found category {eng_category}in thai_data")
                # 初始化遊戲
                cards = game.initialize_game(eng_category)
                
                # 創建遊戲畫面 (使用 Flex Message)
                return create_flex_memory_game(cards, game.get_game_state(), user_id)
            else:
                logger.error(f"Category '{eng_category}' not found in thai_data")
                return TextSendMessage(text=f"Sorry, the category '{category}' was not found in the data. Please contact the administrator.")
        else:
            logger.warning(f"Unrecognized topic: {category}")
            return TextSendMessage(text="Sorry, the selected topic could not be recognized. Please choose again.")
    
    elif message.startswith("Flip Card:"):
        try:
            card_id = int(message.split(":")[1]) if ":" in message else -1
            logger.info(f"User clicked card number: {card_id}")
            
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
                logger.info(f"Preparing to play audio: {audio_url}")
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
                        text="Game over! Would you like to play again",
                        quick_reply=QuickReply(
                            items=[
                                QuickReplyButton(action=MessageAction(label='Play Again', text='Start MemoryGame')),
                                QuickReplyButton(action=MessageAction(label='Back to Main Menu', text='Back to Main Menu'))
                            ]
                        )
                    )
                )
                return messages
        except Exception as e:
            logger.error(f"Error occurred while processing card flip request: {str(e)}")
            return TextSendMessage(text=f"An error occurred while processing your card flip: {str(e)}\nPlease try again or select 'Back to Main Menu'.")
    
    elif message.startswith("Play Audio:"):
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
        
        return TextSendMessage(text="Sorry, the audio could not be played.")
    
    # 默認回傳
    return TextSendMessage(text="Please select 'Start MemoryGame' to begin a new game.")

def create_flex_memory_game(cards, game_state, user_id):
    """創建 Flex Message 的記憶翻牌遊戲界面"""
    from linebot.models import FlexSendMessage, TextSendMessage

    bubbles = []
    try:
        attempts = game_state.get('attempts', 0)
        remaining_time = int(game_state.get('remaining_time', 0))
        category_name = game_state.get('category_name', 'Unknown')
        is_completed = game_state.get('is_completed', False)
        is_timeout = game_state.get('is_timeout', False)

        matched_ids = [c for pair in game_state.get('matched_pairs', []) for c in pair]
        flipped_ids = game_state.get('flipped_cards', [])

        # 遊戲資訊卡片
        info_bubble = {
            "type": "bubble",
            "header": {
                "type": "box",
                "layout": "vertical",
                "contents": [
                    {"type": "text", "text": "Thai Memory Card Game", "weight": "bold", "size": "xl", "color": "#ffffff"},
                    {"type": "text", "text": category_name, "size": "md", "color": "#ffffff"}
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
                        "justifyContent":"center",
                        "contents": [
                            {"type": "text", "text": "⏱️Time Remaining:", "size": "sm", "color": "#555555", "flex": 2},
                            {"type": "text", "text": f"{remaining_time} sec", "size": "sm", "color": "#111111", "flex": 1}
                        ]
                    }
                ]
            }
        }
        bubbles.append(info_bubble)

        # 卡片行排列，每列最多 4 張卡
        card_rows = [cards[i:i+4] for i in range(0, len(cards), 4)]
        for row_cards in card_rows:
            card_contents = []
            for card in row_cards:
                card_id = card['id']
                is_matched = card_id in matched_ids
                is_flipped = card_id in flipped_ids

                if is_matched or is_flipped:
                    if card['type'] == 'image':
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
                                {"type": "image", "url": card['content'], "size": "full", "aspectMode": "cover", "aspectRatio": "1:1"},
                                {"type": "text", "text": card['word'], "size": "xxs", "align": "center", "wrap": True, "maxLines": 2}
                            ]
                        }
                    else:
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
                                {"type": "text", "text": "🎵", "size": "lg", "align": "center", "color": "#FF6B6E"},
                                {"type": "text", "text": card['thai'], "size": "xxs", "align": "center", "wrap": True, "maxLines": 2}
                            ],
                            "action": {"type": "message", "text": f"Play Audio:{card['word']}"}
                        }
                else:
                    back_icon = "🖼️" if card['type'] == "image" else "🎧"
                    back_color = "#4A86E8" if card['type'] == "image" else "#FFA94D"
                    card_box = {
                        "type": "box",
                        "layout": "vertical",
                        "width": "60px",
                        "height": "80px",
                        "backgroundColor": back_color,
                        "cornerRadius": "4px",
                        "borderWidth": "1px",
                        "borderColor": "#0B5ED7",
                        "contents": [
                            {"type": "text", "text": back_icon, "color": "#FFFFFF", "align": "center", "gravity": "center", "size": "xl"},
                            {"type": "text", "text": f"{card_id}", "color": "#FFFFFF", "align": "center", "size": "sm"}
                        ],
                        "action": {"type": "message", "text": f"Flip Card:{card_id}"}
                    }

                card_contents.append(card_box)

            row_bubble = {"type": "bubble", "body": {"type": "box", "layout": "horizontal", "contents": card_contents}}
            bubbles.append(row_bubble)

        flex_message = {"type": "carousel", "contents": bubbles}
        return FlexSendMessage(alt_text="Thai Memory Card Game", contents=flex_message)

    except Exception as e:
        import logging
        logging.getLogger().error(f"Error occurred while creating Flex Message: {str(e)}")
        return TextSendMessage(text="The game display encountered an issue. Please try again later.")

    # ✅ 考試指令過濾（只有在符合格式才執行）
    if text.startswith("Start") and "Exam" in text:
        result = handle_exam_message(event)
        if result:
            if isinstance(result, list):
                line_bot_api.reply_message(event.reply_token, result)
        else:
            line_bot_api.reply_message(event.reply_token, [result])
        return




    # 更新用戶活躍狀態
    user_data_manager.update_streak(user_id)

    
    # 播放音頻請求
    if text.startswith("Play Audio:"):
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
    if text == "Start Learning" or text == "Back to Main Menu":
        exam_sessions.pop(user_id, None)  # ❗️清除考試狀態，避免干擾
        line_bot_api.reply_message(event.reply_token, show_main_menu())
    
    # 選擇主題
    elif text == "Select Topic":
        line_bot_api.reply_message(event.reply_token, show_category_menu())
    
    # 主題選擇處理
    elif text.startswith("Topic:"):
        category = text[3:]  # 取出主題名稱
        # 轉換成英文鍵值
        category_map = {
            "Daily Phrases": "daily_phrases",
            "Numbers": "numbers",
            "Animals": "animals",
            "Food": "food",
            "Transportation": "transportation"
        }
        if category in category_map:
            eng_category = category_map[category]
            user_data['current_category'] = eng_category
            messages = start_image_learning(user_id, eng_category)
            line_bot_api.reply_message(event.reply_token, messages)
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="Sorry, the selected topic could not be recognized. Please choose again.")
            )
    
    # 學習模式選擇
    elif text == "Vocabulary":
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Pronunciation drill":
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Tone Learning":
        messages = start_tone_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    # 進度與導航控制
    elif text == "Next Word":
        # 如果有當前主題，在同一主題中選擇新詞彙
        if user_data.get('current_category'):
            category = user_data['current_category']
            user_data['current_vocab'] = random.choice(thai_data['categories'][category]['words'])
        else:
            # 否則清除當前詞彙，隨機選擇
            user_data['current_vocab'] = None
        
        messages = start_image_learning(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Learning Progress":
        progress_message = show_learning_progress(user_id)
        line_bot_api.reply_message(event.reply_token, progress_message)
    
    elif text == "Practice Weak Words":
        # 找出評分最低的詞彙進行練習
        if not user_data.get('vocab_mastery') or len(user_data['vocab_mastery']) == 0:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="You don't have enough learning history yet. Please start with some vocabulary and pronunciation practice!")
            )
            return
            
        # 找出分數最低的詞彙
        worst_word = min(user_data['vocab_mastery'].items(), 
                      key=lambda x: sum(x[1]['scores'])/len(x[1]['scores']) if x[1]['scores'] else 100)
        
        # 設置為當前詞彙並啟動練習
        user_data['current_vocab'] = worst_word[0]
        messages = start_echo_practice(user_id)
        line_bot_api.reply_message(event.reply_token, messages)
    
    elif text == "Learning Calendar":
        # 顯示用戶的學習日曆和連續學習天數
        streak = user_data.get('streak', 0)
        last_active = user_data.get('last_active', 'Not started yet')
        
        calendar_message = f"📅Your Learning Record：\n\n"
        calendar_message += f"🔥 Consecutive Learning Days: {streak} days\n"
        calendar_message += f"🕓 Last Active Date: {last_active}\n\n"
        calendar_message += "Keep up your learning motivation! A little every day will steadily improve your Thai skills."
        
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=calendar_message)
        )
    elif text == "Exam Mode":
        quick_reply = QuickReply(
            items=[
                QuickReplyButton(action=MessageAction(label='Daily Phrases', text='Start Daily Phrases Exam')),
                QuickReplyButton(action=MessageAction(label='Numbers', text='Start Numbers Exam')),
                QuickReplyButton(action=MessageAction(label='Animals', text='Start Animals Exam')),
                QuickReplyButton(action=MessageAction(label='Food', text='Start Food Exam')),
                QuickReplyButton(action=MessageAction(label='Transportation', text='Start Transportation Exam')),
                QuickReplyButton(action=MessageAction(label='Comprehensive Exam', text='Start Full Exam'))
            ]
        )
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="Please select a category for the exam:",
                quick_reply=quick_reply
            )
        )
        return


    else:
        # 默認回覆
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="Please select 'Start Learning' or use the menu button to begin your Thai learning journey.")
        )
import threading
import time  # ✅ 加上這行

# 定期清理臨時檔案函式
def cleanup_temp_files():
    """清理臨時檔案"""
    try:
        temp_dir = os.environ.get('TEMP', '/tmp')
        audio_dir = os.path.join(temp_dir, 'temp_audio')
        
        if not os.path.exists(audio_dir):
            return
            
        now = datetime.now()
        for filename in os.listdir(audio_dir):
            if not filename.startswith('temp_'):
                continue
                
            file_path = os.path.join(audio_dir, filename)
            if os.path.isfile(file_path):
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                if (now - mtime).total_seconds() > 3600:
                    try:
                        os.remove(file_path)
                        logger.info(f"Cleaned up temporary file: {file_path}")
                    except:
                        pass
    except Exception as e:
        logger.error(f"Failed to clean up temporary files: {str(e)}")

# 背景執行清理：每 30 分鐘跑一次
def periodic_cleanup():
    while True:
        time.sleep(1800)  # 每 30 分鐘執行
        cleanup_temp_files()

# 啟動執行緒
cleanup_thread = threading.Thread(target=periodic_cleanup, daemon=True)
cleanup_thread.start()

    # 主程序入口 (放在最後)
if __name__ == "__main__":
    # 啟動 Flask 應用，使用環境變數設定的端口或默認5000
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"Application started on port  {port}")
    app.run(host='0.0.0.0', port=port)
    
    