# encoding:utf-8

import time

import openai
import openai.error
import requests
from bot.bot import Bot
from bot.chatgpt.chat_gpt_session import ChatGPTSession
from bot.openai.open_ai_image import OpenAIImage
from bot.session_manager import SessionManager
from bridge.context import ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.token_bucket import TokenBucket
from config import conf, load_config
import json

# OpenAI对话模型API (可用)
class ChatGPTBot(Bot, OpenAIImage):
    def __init__(self):
        super().__init__()
        # set the default api_key
        openai.api_key = conf().get("open_ai_api_key")
        if conf().get("open_ai_api_base"):
            openai.api_base = conf().get("open_ai_api_base")
        proxy = conf().get("proxy")
        if proxy:
            openai.proxy = proxy
        if conf().get("rate_limit_chatgpt"):
            self.tb4chatgpt = TokenBucket(conf().get("rate_limit_chatgpt", 20))

        self.sessions = SessionManager(
            ChatGPTSession, model=conf().get("model") or "gpt-3.5-turbo"
        )
        self.args = {
            "model": conf().get("model") or "gpt-3.5-turbo",  # 对话模型的名称
            "temperature": conf().get("temperature", 0.9),  # 值在[0,1]之间，越大表示回复越具有不确定性
            # "max_tokens":4096,  # 回复最大的字符数
            "top_p": 1,
            "frequency_penalty": conf().get(
                "frequency_penalty", 0.0
            ),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "presence_penalty": conf().get(
                "presence_penalty", 0.0
            ),  # [-2,2]之间，该值越大则更倾向于产生不同的内容
            "request_timeout": conf().get(
                "request_timeout", None
            ),  # 请求超时时间，openai接口默认设置为600，对于难问题一般需要较长时间
            "timeout": conf().get("request_timeout", None),  # 重试超时时间，在这个时间内，将会自动重试
        }

    def reply(self, query, context=None):
        # acquire reply content
        if context.type == ContextType.TEXT:
            logger.info("[CHATGPT] query={}".format(query))

            session_id = context["session_id"]
            reply = None
            clear_memory_commands = conf().get("clear_memory_commands", ["#清除记忆"])
            if query in clear_memory_commands:
                self.sessions.clear_session(session_id)
                reply = Reply(ReplyType.INFO, "记忆已清除")
            elif query == "#清除所有":
                self.sessions.clear_all_session()
                reply = Reply(ReplyType.INFO, "所有人记忆已清除")
            elif query == "#更新配置":
                load_config()
                reply = Reply(ReplyType.INFO, "配置已更新")
            if reply:
                return reply
            session = self.sessions.session_query(query, session_id)
            logger.debug("[CHATGPT] session query={}".format(session.messages))

            api_key = conf().get('open_ai_api_key')

            # if context.get('stream'):
            #     # reply in stream
            #     return self.reply_text_stream(query, new_query, session_id)

            reply_content = self.reply_text(session, api_key)

            logger.debug(
                "[CHATGPT] new_query={}, session_id={}, reply_cont={}, completion_tokens={}".format(
                    session.messages,
                    session_id,
                    reply_content["content"],
                    reply_content["completion_tokens"],
                )
            )
            if (
                reply_content["completion_tokens"] == 0
                and len(reply_content["content"]) > 0
            ):
                reply = Reply(ReplyType.ERROR, reply_content["content"])
            elif reply_content["completion_tokens"] > 0:
                self.sessions.session_reply(
                    reply_content["content"], session_id, reply_content["total_tokens"]
                )
                reply = Reply(ReplyType.TEXT, reply_content["content"])
            else:
                reply = Reply(ReplyType.ERROR, reply_content["content"])
                logger.debug("[CHATGPT] reply {} used 0 tokens.".format(reply_content))
            return reply

        elif context.type == ContextType.IMAGE_CREATE:
            ok, retstring = self.create_img(query, 0)
            reply = None
            if ok:
                reply = Reply(ReplyType.IMAGE_URL, retstring)
            else:
                reply = Reply(ReplyType.ERROR, retstring)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    def reply_text(self, session: ChatGPTSession, api_key=None, retry_count=0) -> dict:
        """
        call openai's ChatCompletion to get the answer
        :param session: a conversation session
        :param session_id: session id
        :param retry_count: retry count
        :return: {}
        """

        try:
            start_time = time.time()  # 获取当前时间

            isOpenAI = conf().get("is_openAI");
            if conf().get("rate_limit_chatgpt") and not self.tb4chatgpt.get_token():
                raise openai.error.RateLimitError("RateLimitError: rate limit exceeded")
            # if api_key == None, the default openai.api_key will be used
            if conf().get("distribute_url") and not isOpenAI:
                clientId =conf().get("client_id")
                data=self.args
                data['messages']=session.messages
                headers = {
                    'Content-Type': 'application/json',
                    'client-id': clientId,
                    'people-desuka': 'robots'
                    # 如果还有其他的headers，你可以在这里添加
                }
                logger.info("开始请求ChatGPT了")
                # 发送POST请求
                distributeUrl= conf().get("distribute_url") +'/openAI/v1/chat/completions';

                response = requests.post(distributeUrl, headers=headers, data=json.dumps(data),timeout=600)
                res = json.loads(response.text)
                response=res['data']
                return {
                "total_tokens": response["usage"]["total_tokens"],
                "completion_tokens": response["usage"]["completion_tokens"],
                "content": response['choices'][0]["message"]["content"],
                }
            else:
                response = openai.ChatCompletion.create(
                    api_key=api_key, messages=session.messages, **self.args
                )
            # logger.info("[ChatGPT] reply={}, total_tokens={}".format(response.choices[0]['message']['content'], response["usage"]["total_tokens"]))
                return {
                    "total_tokens": response["usage"]["total_tokens"],
                    "completion_tokens": response["usage"]["completion_tokens"],
                    "content": response.choices[0]["message"]["content"],
                }
        except Exception as e:
            logger.info("出错了:",e)
            need_retry = retry_count < 2
            result = {"completion_tokens": 0, "content": "我现在有点累了，等会再来吧"}
            if isinstance(e, openai.error.RateLimitError):
                logger.warn("[CHATGPT] RateLimitError: {}".format(e))
                logger.error("[CHATGPT] 提问太快说明key繁忙，需要重新获取key: {}".format(e))
                # api_key = self.getNewKey(api_key)
                result["content"] = "提问太快啦，请休息一下再问我吧"
                need_retry= True;
                if need_retry:
                    time.sleep(20)
            elif isinstance(e, openai.error.Timeout):
                logger.warn("[CHATGPT] Timeout: {}".format(e))
                result["content"] = "我没有收到你的消息"
                if need_retry:
                    time.sleep(5)
            elif isinstance(e, openai.error.APIConnectionError):
                logger.warn("[CHATGPT] APIConnectionError: {}".format(e))
                need_retry = False
                result["content"] = "我连接不到你的网络"
            elif isinstance(e, openai.error.AuthenticationError):
                logger.error("[OPEN_AI] AuthenticationError重新获取openkey: {}".format(e))
                # api_key = self.getNewKey(api_key)
                if retry_count > 3:
                 need_retry=False
                 logger.error("获取openAIkey值异常")
                else:
                 need_retry = True
                result[2]= '重新获取openkey'

            else:
                logger.warn("[CHATGPT] Exception: {}".format(e))
                need_retry = False
                self.sessions.clear_session(session.session_id)

            if need_retry:
                logger.warn("[CHATGPT] 第{}次重试".format(retry_count + 1))
                return self.reply_text(session, api_key, retry_count + 1)
            else:
                return result

        finally:
            end_time = time.time()  # 获取当前时间
            elapsed_time = end_time - start_time  # 计算执行时间
            # 不论异常是否发生，这里的代码都将被执行
            logger.info(f"Execution time: {elapsed_time} seconds")

    def getNewKey(self, api_key):
        from config import load_openai_key, config, get_remote_api_key, modifyLoad, setOpenAiKey
        modifyLoad()
        new_key = get_remote_api_key(config['distribute_url'], config['client_id'],
                                     config['open_ai_api_key'])
        conf().set('open_ai_api_key', new_key)
        api_key = conf().get('open_ai_api_key')
        setOpenAiKey(api_key)
        return api_key


class AzureChatGPTBot(ChatGPTBot):
    def __init__(self):
        super().__init__()
        openai.api_type = "azure"
        openai.api_version = "2023-03-15-preview"
        self.args["deployment_id"] = conf().get("azure_deployment_id")
