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

logger.info(f"初始化應用程式... LINE Bot 和 Azure Speech 服務已配置")

# === 用戶數據管理 ===
class UserData:
    def __init__(self):
        self.users = {}
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
               'audio_url': 'https://example.com/audio/sawatdee.mp3',
               'image_url': 'https://example.com/images/greeting.jpg'},
        '謝謝': {'thai': 'ขอบคุณ', 'pronunciation': 'khop-khun', 'tone': 'low-mid',
               'audio_url': 'https://example.com/audio/kopkhun.mp3',
               'image_url': 'https://example.com/images/thanks.jpg'},
        '再見': {'thai': 'ลาก่อน', 'pronunciation': 'la-kon', 'tone': 'mid-mid',
               'audio_url': 'https://example.com/audio/lakon.mp3',
               'image_url': 'https://example.com/images/goodbye.jpg'},
        '對不起': {'thai': 'ขอโทษ', 'pronunciation': 'kho-thot', 'tone': 'low-low',
                'audio_url': 'https://example.com/audio/khotot.mp3',
                'image_url': 'https://example.com/images/sorry.jpg'},
        '早安': {'thai': 'อรุณสวัสดิ์', 'pronunciation': 'a-run-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://example.com/audio/arunsawat.mp3',
              'image_url': 'https://example.com/images/morning.jpg'},
        '晚安': {'thai': 'ราตรีสวัสดิ์', 'pronunciation': 'ra-tree-sa-wat', 'tone': 'mid-mid-falling-mid',
              'audio_url': 'https://example.com/audio/ratreesawat.mp3',
              'image_url': 'https://example.com/images/night.jpg'},
        '不客氣': {'thai': 'ไม่เป็นไร', 'pronunciation': 'mai-pen-rai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://example.com/audio/maipenrai.mp3',
                'image_url': 'https://example.com/images/yourewelcome.jpg'},
        '怎麼走？': {'thai': 'ไปทางไหน', 'pronunciation': 'pai-tang-nai', 'tone': 'mid-mid-mid',
                'audio_url': 'https://example.com/audio/paitangnai.mp3',
                'image_url': 'https://example.com/images/howtoget.jpg'},
        '多少錢': {'thai': 'เท่าไหร่', 'pronunciation': 'tao-rai', 'tone': 'mid-mid',
               'audio_url': 'https://example.com/audio/taorai.mp3',
               'image_url': 'https://example.com/images/price.jpg'},
        '好吃': {'thai': 'อร่อย', 'pronunciation': 'a-roi', 'tone': 'mid-mid',
               'audio_url': 'https://example.com/audio/aroi.mp3',
               'image_url': 'https://example.com/images/delicious.jpg'},
        
        # 數字
        '一': {'thai': 'หนึ่ง', 'pronunciation': 'neung', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/neung.mp3',
             'image_url': 'https://example.com/images/one.jpg'},
        '二': {'thai': 'สอง', 'pronunciation': 'song', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/song.mp3',
             'image_url': 'https://example.com/images/two.jpg'},
        '三': {'thai': 'สาม', 'pronunciation': 'sam', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/sam.mp3',
             'image_url': 'https://example.com/images/three.jpg'},
        '四': {'thai': 'สี่', 'pronunciation': 'see', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/see.mp3',
             'image_url': 'https://example.com/images/four.jpg'},
        '五': {'thai': 'ห้า', 'pronunciation': 'ha', 'tone': 'falling',
             'audio_url': 'https://example.com/audio/ha.mp3',
             'image_url': 'https://example.com/images/five.jpg'},
        '六': {'thai': 'หก', 'pronunciation': 'hok', 'tone': 'low',
             'audio_url': 'https://example.com/audio/hok.mp3',
             'image_url': 'https://example.com/images/six.jpg'},
        '七': {'thai': 'เจ็ด', 'pronunciation': 'jet', 'tone': 'falling',
             'audio_url': 'https://example.com/audio/jet.mp3',
             'image_url': 'https://example.com/images/seven.jpg'},
        '八': {'thai': 'แปด', 'pronunciation': 'paet', 'tone': 'falling',
             'audio_url': 'https://example.com/audio/paet.mp3',
             'image_url': 'https://example.com/images/eight.jpg'},
        '九': {'thai': 'เก้า', 'pronunciation': 'kao', 'tone': 'falling',
             'audio_url': 'https://example.com/audio/kao.mp3',
             'image_url': 'https://example.com/images/nine.jpg'},
        '十': {'thai': 'สิบ', 'pronunciation': 'sip', 'tone': 'low',
             'audio_url': 'https://example.com/audio/sip.mp3',
             'image_url': 'https://example.com/images/ten.jpg'},
        
        # 動物
        '貓': {'thai': 'แมว', 'pronunciation': 'maew', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/maew.mp3',
             'image_url': 'https://example.com/images/cat.jpg'},
        '狗': {'thai': 'หมา', 'pronunciation': 'ma', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/ma.mp3',
             'image_url': 'https://example.com/images/dog.jpg'},
        '鳥': {'thai': 'นก', 'pronunciation': 'nok', 'tone': 'low',
             'audio_url': 'https://example.com/audio/nok.mp3',
             'image_url': 'https://example.com/images/bird.jpg'},
        '魚': {'thai': 'ปลา', 'pronunciation': 'pla', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/pla.mp3',
             'image_url': 'https://example.com/images/fish.jpg'},
        '大象': {'thai': 'ช้าง', 'pronunciation': 'chang', 'tone': 'high',
              'audio_url': 'https://example.com/audio/chang.mp3',
              'image_url': 'https://example.com/images/elephant.jpg'},
        '老虎': {'thai': 'เสือ', 'pronunciation': 'suea', 'tone': 'low',
              'audio_url': 'https://example.com/audio/suea.mp3',
              'image_url': 'https://example.com/images/tiger.jpg'},
        '猴子': {'thai': 'ลิง', 'pronunciation': 'ling', 'tone': 'mid',
              'audio_url': 'https://example.com/audio/ling.mp3',
              'image_url': 'https://example.com/images/monkey.jpg'},
        '雞': {'thai': 'ไก่', 'pronunciation': 'kai', 'tone': 'low',
             'audio_url': 'https://example.com/audio/kai.mp3',
             'image_url': 'https://example.com/images/chicken.jpg'},
        '豬': {'thai': 'หมู', 'pronunciation': 'moo', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/moo.mp3',
             'image_url': 'https://example.com/images/pig.jpg'},
        '牛': {'thai': 'วัว', 'pronunciation': 'wua', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/wua.mp3',
             'image_url': 'https://example.com/images/cow.jpg'},
        
        # 食物
        '米飯': {'thai': 'ข้าว', 'pronunciation': 'khao', 'tone': 'falling',
              'audio_url': 'https://example.com/audio/khao.mp3',
              'image_url': 'https://example.com/images/rice.jpg'},
        '麵': {'thai': 'ก๋วยเตี๋ยว', 'pronunciation': 'guay-tiew', 'tone': 'falling-falling-low',
             'audio_url': 'https://example.com/audio/guaytiew.mp3',
             'image_url': 'https://example.com/images/noodle.jpg'},
        '啤酒': {'thai': 'เบียร์', 'pronunciation': 'bia', 'tone': 'mid',
              'audio_url': 'https://example.com/audio/bia.mp3',
              'image_url': 'https://example.com/images/beer.jpg'},
        '麵包': {'thai': 'ขนมปัง', 'pronunciation': 'kha-nom-pang', 'tone': 'mid-mid-mid',
              'audio_url': 'https://example.com/audio/khanompang.mp3',
              'image_url': 'https://example.com/images/bread.jpg'},
        '雞翅': {'thai': 'ปีกไก่', 'pronunciation': 'peek-kai', 'tone': 'falling-low',
              'audio_url': 'https://example.com/audio/peekkai.mp3',
              'image_url': 'https://example.com/images/chickenwing.jpg'},
        '芒果糯米飯': {'thai': 'ข้าวเหนียวมะม่วง', 'pronunciation': 'khao-niew-ma-muang', 'tone': 'falling-falling-mid-mid',
                 'audio_url': 'https://example.com/audio/khaoniewmamuang.mp3',
                 'image_url': 'https://example.com/images/mangostickyrice.jpg'},
        '炒飯': {'thai': 'ข้าวผัด', 'pronunciation': 'khao-pad', 'tone': 'falling-low',
              'audio_url': 'https://example.com/audio/khaopad.mp3',
              'image_url': 'https://example.com/images/friedrice.jpg'},
        '青木瓜沙拉': {'thai': 'ส้มตำ', 'pronunciation': 'som-tam', 'tone': 'falling-mid',
                  'audio_url': 'https://example.com/audio/somtam.mp3',
                  'image_url': 'https://example.com/images/papayasalad.jpg'},
        '冬蔭功湯': {'thai': 'ต้มยำกุ้ง', 'pronunciation': 'tom-yum-kung', 'tone': 'high-mid-mid',
                 'audio_url': 'https://example.com/audio/tomyumkung.mp3',
                 'image_url': 'https://example.com/images/tomyumkung.jpg'},
        '泰式炒河粉': {'thai': 'ผัดไทย', 'pronunciation': 'pad-thai', 'tone': 'low-mid',
                  'audio_url': 'https://example.com/audio/padthai.mp3',
                  'image_url': 'https://example.com/images/padthai.jpg'},
        
        # 交通工具
        '車子': {'thai': 'รถยนต์', 'pronunciation': 'rot-yon', 'tone': 'high-mid',
              'audio_url': 'https://example.com/audio/rotyon.mp3',
              'image_url': 'https://example.com/images/car.jpg'},
        '公車': {'thai': 'รถเมล์', 'pronunciation': 'rot-mae', 'tone': 'high-mid',
              'audio_url': 'https://example.com/audio/rotmae.mp3',
              'image_url': 'https://example.com/images/bus.jpg'},
        '計程車': {'thai': 'แท็กซี่', 'pronunciation': 'taxi', 'tone': 'mid-mid',
               'audio_url': 'https://example.com/audio/taxi.mp3',
               'image_url': 'https://example.com/images/taxi.jpg'},
        '摩托車': {'thai': 'มอเตอร์ไซค์', 'pronunciation': 'motor-sai', 'tone': 'mid-mid-mid',
               'audio_url': 'https://example.com/audio/motorsai.mp3',
               'image_url': 'https://example.com/images/motorcycle.jpg'},
        '火車': {'thai': 'รถไฟ', 'pronunciation': 'rot-fai', 'tone': 'high-mid',
              'audio_url': 'https://example.com/audio/rotfai.mp3',
              'image_url': 'https://example.com/images/train.jpg'},
        '飛機': {'thai': 'เครื่องบิน', 'pronunciation': 'krueang-bin', 'tone': 'falling-mid',
              'audio_url': 'https://example.com/audio/krueangbin.mp3',
              'image_url': 'https://example.com/images/airplane.jpg'},
        '船': {'thai': 'เรือ', 'pronunciation': 'ruea', 'tone': 'mid',
             'audio_url': 'https://example.com/audio/ruea.mp3',
             'image_url': 'https://example.com/images/boat.jpg'},
        '腳踏車': {'thai': 'จักรยาน', 'pronunciation': 'jak-ka-yan', 'tone': 'low-low-mid',
               'audio_url': 'https://example.com/audio/jakkayan.mp3',
               'image_url': 'https://example.com/images/bicycle.jpg'},
        '嘟嘟車': {'thai': 'ตุ๊กตุ๊ก', 'pronunciation': 'tuk-tuk', 'tone': 'high-high',
               'audio_url': 'https://example.com/audio/tuktuk.mp3',
               'image_url': 'https://example.com/images/tuktuk.jpg'},
        '貨車': {'thai': 'รถบรรทุก', 'pronunciation': 'rot-ban-tuk', 'tone': 'high-mid-low',
              'audio_url': 'https://example.com/audio/rotbantuk.mp3',
              'image_url': 'https://example.com/images/truck.jpg'}
    },
    'dialogues': {
        '打招呼': [
            {'zh': '你好', 'thai': 'สวัสดี', 'pronunciation': 'sa-wat-dee'},
            {'zh': '你好嗎？', 'thai': 'สบายดีไหม', 'pronunciation': 'sa-bai-dee-mai'},
            {'zh': '我很好，謝謝', 'thai': 'สบายดี ขอบคุณ', 'pronunciation': 'sa-bai-dee khop-khun'}
        ],
        '點餐': [
            {'zh': '這個多少錢？', 'thai': 'อันนี้เท่าไหร่', 'pronunciation': 'an-nee-tao-rai'},
            {'zh': '我要這個', 'thai': 'เอาอันนี้', 'pronunciation': 'ao-an-nee'},
            {'zh': '太貴了', 'thai': 'แพงเกินไป', 'pronunciation': 'paeng-gern-pai'}
        ],
        '購物': [
            {'zh': '便宜一點', 'thai': 'ลดราคาหน่อย', 'pronunciation': 'lot-ra-ka-noi'},
            {'zh': '我買這個', 'thai': 'ผมซื้ออันนี้/ฉันซื้ออันนี้', 'pronunciation': 'phom/chan sue an-nee'},
            {'zh': '有折扣嗎？', 'thai': 'มีส่วนลดไหม', 'pronunciation': 'mee-suan-lot-mai'}
        ]
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
            'dialogue': '打招呼'
        },
        {
            'day': 2, 
            'theme': '基本禮貌用語',
            'words': ['對不起', '謝謝', '不客氣'],
            'dialogue': '打招呼'
        },
        {
            'day': 3, 
            'theme': '購物短語',
            'words': ['多少錢', '太貴了', '便宜一點'],
            'dialogue': '購物'
        }
    ]
}

logger.info("已載入泰語學習資料")

# === 輔助函數 ===
def get_audio_content(message_id):
    """從LINE取得音訊內容"""
    logger.info(f"獲取音訊內容，訊息ID: {message_id}")
    message_content = line_bot_api.get_message_content(message_id)
    audio_content = b''
    for chunk in message_content.iter_content():
        audio_content += chunk
    return audio_content

def process_audio_content(audio_content):
    """處理音頻內容並轉換為適合語音識別的格式"""
    try:
        # 創建臨時目錄
        temp_dir = os.environ.get('TEMP', '/tmp')
        audio_dir = os.path.join(temp_dir, 'temp_audio')
        os.makedirs(audio_dir, exist_ok=True)
        
        # 生成唯一的文件名
        temp_m4a = os.path.join(audio_dir, f'temp_{uuid.uuid4()}.m4a')
        temp_wav = os.path.join(audio_dir, f'temp_{uuid.uuid4()}.wav')
        
        logger.info(f"保存原始音頻到 {temp_m4a}")
        # 保存原始音頻
        with open(temp_m4a, 'wb') as f:
            f.write(audio_content)
        
        logger.info("使用 pydub 轉換音頻格式")
        # 使用 pydub 轉換格式
        audio = AudioSegment.from_file(temp_m4a)
        audio = audio.set_frame_rate(16000).set_channels(1)
        audio.export(temp_wav, format='wav')
        
        # 清除原始文件
        try:
            os.remove(temp_m4a)
            logger.info(f"已清除臨時文件 {temp_m4a}")
        except Exception as e:
            logger.warning(f"清除臨時文件失敗: {str(e)}")
            pass
            
        return temp_wav
    except Exception as e:
        logger.error(f"音頻處理錯誤: {str(e)}")
        # 如果處理失敗，返回原始音頻
        temp_path = os.path.join(audio_dir, f'original_{uuid.uuid4()}.bin')
        with open(temp_path, 'wb') as f:
            f.write(audio_content)
        return temp_path

def evaluate_pronunciation(audio_file_path, reference_text, language="th-TH"):
    """使用Azure Speech Services進行發音評估"""
    try:
        logger.info(f"開始發音評估，參考文本: {reference_text}")
        # 設定語音配置
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        speech_config.speech_recognition_language = language
        
        # 設定發音評估配置
        pronunciation_config = speechsdk.PronunciationAssessmentConfig(
            reference_text=reference_text,
            grading_system=speechsdk.PronunciationAssessmentGradingSystem.HundredMark,
            granularity=speechsdk.PronunciationAssessmentGranularity.Phoneme,
            enable_miscue=True
        )
        
        # 設定音訊輸入
        audio_config = speechsdk.audio.AudioConfig(filename=audio_file_path)
        
        # 創建語音識別器
        speech_recognizer = speechsdk.SpeechRecognizer(
            speech_config=speech_config, 
            audio_config=audio_config
        )
        
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
            
            logger.info(f"發音評估完成，總分: {overall_score}")
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
            logger.warning(f"語音識別失敗，原因: {result.reason}")
            return {
                "success": False,
                "error": "無法識別語音",
                "result_reason": result.reason
            }
    
    except Exception as e:
        logger.error(f"發音評估過程中發生錯誤: {str(e)}")
        return {
            "success": False,
            "error": str(e)
        }
    finally:
        # 清理臨時檔案
        if os.path.exists(audio_file_path):
            try:
                os.remove(audio_file_path)
                logger.info(f"已清除臨時檔案 {audio_file_path}")
            except Exception as e:
                logger.warning(f"清除臨時檔案失敗: {str(e)}")
                pass

# === 學習功能模塊 ===

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
            text=f"請聽標準發音，然後跟著練習：\n\n泰語：{word_data['thai']}\n發音：{word_data['pronunciation']}\n\n請點擊下方麥克風按鈕錄製您的發音"
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
    
    # 添加錄音引導
    buttons_template = ButtonsTemplate(
        title="發音練習",
        text="請點擊錄音按鈕開始錄音",
        actions=[
            URIAction(
                label="開始錄音",
                uri="line://nv/camera/speech"
            ),
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

def start_dialogue_practice(user_id, dialogue_key=None):
    """啟動對話練習模式"""
    logger.info(f"啟動對話練習模式，用戶ID: {user_id}")
    user_data = user_data_manager.get_user_data(user_id)
    user_data['current_activity'] = 'dialogue_practice'
    
    # 如果沒有指定對話，隨機選擇一個
    if not dialogue_key:
        dialogue_key = random.choice(list(thai_data['dialogues'].keys()))
    
    user_data['current_dialogue'] = dialogue_key
    dialogue = thai_data['dialogues'][dialogue_key]
    logger.info(f"選擇對話主題: {dialogue_key}")
    
    # 建立訊息列表
    message_list = []
    
    # 對話介紹
    message_list.append(
        TextSendMessage(text=f"對話主題：{dialogue_key}\n\n請學習以下對話：")
    )
    
    # 對話內容
    dialogue_text = ""
    for i, line in enumerate(dialogue):
        dialogue_text += f"{i+1}. {line['zh']} - {line['thai']} ({line['pronunciation']})\n"
    
    message_list.append(TextSendMessage(text=dialogue_text))
    
    # 添加選項按鈕
    buttons_template = ButtonsTemplate(
        title="對話練習",
        text="請選擇操作",
        actions=[
            MessageAction(label="練習對話", text="練習對話"),
            MessageAction(label="下一個對話", text="下一個對話"),
            MessageAction(label="返回主選單", text="返回主選單")
        ]
    )
    message_list.append(
        TemplateSendMessage(alt_text="對話練習選項", template=buttons_template)
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
            QuickReplyButton(action=MessageAction(label='對話練習', text='對話練習')),
            QuickReplyButton(action=MessageAction(label='學習進度', text='學習進度'))
        ]
    )
    
    return TextSendMessage(
        text="🇹🇭 歡迎使用泰語學習系統 🇹🇭\n請選擇您想要的學習模式：",
        quick_reply=quick_reply
    )

# === LINE Bot Webhook 處理 ===
@app.route("/callback", methods=['POST'])
def callback():
    """處理LINE Webhook回調"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        logger.error("無效的簽名")
        abort(400)
        
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    """處理文字訊息"""
    text = event.message.text
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的文字訊息: {text}")
    
    # 更新用戶活躍狀態
    user_data_manager.update_streak(user_id)
    
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
    
    elif text == "對話練習":
        messages = start_dialogue_practice(user_id)
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
    
    elif text == "下一個對話":
        # 清除當前對話，開始新一輪對話練習
        messages = start_dialogue_practice(user_id)
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
    
    elif text == "練習對話":
        # 如果用戶已經有當前對話，發送練習提示
        if user_data.get('current_dialogue'):
            dialogue_key = user_data['current_dialogue']
            dialogue = thai_data['dialogues'][dialogue_key]
            
            practice_tips = f"請嘗試記住並練習以下對話：\n\n"
            for i, line in enumerate(dialogue):
                practice_tips += f"{i+1}. {line['zh']} - {line['thai']}\n"
            
            practice_tips += "\n提示：先遮住泰語部分，嘗試翻譯中文句子，然後對照檢查。"
            
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=practice_tips)
            )
        else:
            # 否則開始新的對話練習
            messages = start_dialogue_practice(user_id)
            line_bot_api.reply_message(event.reply_token, messages)
    
    else:
        # 默認回覆
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請選擇「開始學習」或點擊選單按鈕開始泰語學習之旅")
        )

@handler.add(MessageEvent, message=AudioMessage)
def handle_audio_message(event):
    """處理音頻消息，主要用於發音評估"""
    user_id = event.source.user_id
    user_data = user_data_manager.get_user_data(user_id)
    
    logger.info(f"收到用戶 {user_id} 的音頻訊息")
    
    # 檢查用戶是否在發音練習中
    if user_data.get('current_activity') == 'echo_practice':
        try:
            # 獲取音訊內容
            audio_content = get_audio_content(event.message.id)
            
            # 獲取當前詞彙
            word_key = user_data.get('current_vocab')
            if not word_key:
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(text="請先選擇一個詞彙進行學習")
                )
                return
                
            word_data = thai_data['basic_words'][word_key]
            
            # 處理音頻並轉換格式
            audio_file_path = process_audio_content(audio_content)
            
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
                        'last_practiced': datetime.now().strftime("%Y-%m-%d")
                    }
                else:
                    user_data['vocab_mastery'][word_key]['practice_count'] += 1
                    user_data['vocab_mastery'][word_key]['scores'].append(score)
                    user_data['vocab_mastery'][word_key]['last_practiced'] = datetime.now().strftime("%Y-%m-%d")
                
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
            # 處理例外
            logger.error(f"處理音頻時發生錯誤: {str(e)}")
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=f"處理您的錄音時發生錯誤：{str(e)}\n請重新嘗試或聯繫系統管理員。")
            )
    else:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="請先選擇「練習發音」開始發音練習")
        )

# 測試 Azure 語音服務連接
def test_azure_connection():
    """測試 Azure 語音服務連接"""
    try:
        speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=speech_region)
        logger.info("Azure Speech Services 連接測試成功")
    except Exception as e:
        logger.error(f"Azure Speech Services 連接測試失敗: {str(e)}")

# 直接在模組層級調用這個函數
test_azure_connection()

if __name__ == "__main__":
    # 啟動 Flask 應用，使用環境變數設定的端口或默認5000
    port = int(os.environ.get('PORT', 5000))
    logger.info(f"應用啟動在端口 {port}")
    app.run(host='0.0.0.0', port=port)