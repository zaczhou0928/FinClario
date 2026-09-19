"""轻量本地适配器，供演示和单机 LLM 模式使用；保留原组件接口。"""
import asyncio
import json
import re
from datetime import date
from finance.store import DATA_DIR
from memory.conversation_memory import MemoryContext, Message, MsgRole


def tokens(text):
    text=str(text).lower()
    words=set(re.findall(r'[a-z0-9]{2,}',text))
    chinese=''.join(re.findall(r'[\u4e00-\u9fff]+',text))
    words.update(chinese[i:i+2] for i in range(max(0,len(chinese)-1)))
    return words


class LocalKnowledgeBase:
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS knowledge(id TEXT PRIMARY KEY,data TEXT)')
        self.add_documents(json.loads((DATA_DIR/'knowledge.json').read_text()), seed_only=True)

    def add_documents(self,documents,seed_only=False):
        from mcp.knowledge_base import validate_document
        documents=[validate_document(d) for d in documents]
        with self.store.connect() as db:
            for d in documents:
                sql='INSERT OR IGNORE INTO knowledge VALUES (?,?)' if seed_only else 'INSERT OR REPLACE INTO knowledge VALUES (?,?)'
                db.execute(sql,(d['doc_id'],json.dumps(d,ensure_ascii=False)))
        return len(documents)

    async def add_documents_async(self,documents):
        return await asyncio.to_thread(self.add_documents,documents)

    async def doc_count_async(self):
        with self.store.connect() as db:
            return db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0]

    async def search_handler(self,params,context):
        query=params.get('query','')
        terms=tokens(query)
        product_ids=set(re.findall(r'EM\d{3}',query.upper()))
        today=self.store.as_of or date.today().isoformat()
        with self.store.connect() as db:
            docs=[json.loads(r['data']) for r in db.execute('SELECT data FROM knowledge')]
        found=[]
        for d in docs:
            if d['source_type']=='unverified' or not d['effective_from'] <= today <= d['effective_to']:
                continue
            if product_ids and d['product_id'] not in product_ids|{'ALL'}:
                continue
            title_terms=tokens(d['title'])
            overlap=len(terms & tokens(d['content']+' '+d['title']))
            score=(overlap+2*len(terms&title_terms))/max(1,len(terms))
            if product_ids and d['product_id'] in product_ids: score+=2
            if overlap or (product_ids and d['product_id'] in product_ids):
                found.append(dict(d,score=round(score,4),retrieval='local_lexical'))
        return sorted(found,key=lambda d:d['score'],reverse=True)[:max(1,min(10,int(params.get('top_k',5))))]


class LocalMemoryManager:
    """SQLite 持久会话；最近 20 条 + 历史摘要 + 受限偏好，无外部服务。"""
    def __init__(self,store):
        self.store=store
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,user_id TEXT,conv_id TEXT,role TEXT,content TEXT);
                CREATE TABLE IF NOT EXISTS local_profiles(user_id TEXT PRIMARY KEY,data TEXT);
            ''')

    async def get_context(self,user_id,conv_id,query=''):
        with self.store.connect() as db:
            rows=db.execute('SELECT role,content FROM messages WHERE user_id=? AND conv_id=? ORDER BY id DESC LIMIT 40',(user_id,conv_id)).fetchall()
            p=db.execute('SELECT data FROM local_profiles WHERE user_id=?',(user_id,)).fetchone()
        rows=list(reversed(rows))
        recent=[Message(MsgRole(r['role']),r['content']) for r in rows[-20:]]
        summary='历史对话片段（不能作为当前业务状态）：'+ '\n'.join(r['content'][:100] for r in rows[:-20]) if len(rows)>20 else ''
        return MemoryContext(recent,[],json.loads(p['data']) if p else {},summary)

    async def add_message(self,user_id,conv_id,role,content,metadata=None):
        with self.store.connect() as db:
            db.execute('INSERT INTO messages(user_id,conv_id,role,content) VALUES (?,?,?,?)',(user_id,conv_id,role.value,content))
        if role==MsgRole.USER:
            await self.update_profile(user_id,conv_id)

    async def update_profile(self,user_id,conv_id):
        from memory.conversation_memory import extract_communication_preferences
        with self.store.connect() as db:
            rows=db.execute("SELECT content FROM messages WHERE user_id=? AND conv_id=? AND role='user' ORDER BY id DESC LIMIT 10",(user_id,conv_id)).fetchall()
            old=db.execute('SELECT data FROM local_profiles WHERE user_id=?',(user_id,)).fetchone()
            profile=json.loads(old['data']) if old else {}
            for r in reversed(rows): profile.update(extract_communication_preferences(r['content']))
            db.execute('INSERT OR REPLACE INTO local_profiles VALUES (?,?)',(user_id,json.dumps(profile)))

    async def close(self):
        pass
