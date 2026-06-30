<script setup>
import { ref } from 'vue'
const message = ref(''), rows = ref([]), busy = ref(false), error = ref('')
const conv = ref('')
async function send() {
  if (!message.value.trim() || busy.value) return
  const input = message.value; message.value = ''; busy.value = true; error.value = ''
  rows.value.push({ role: '你', content: input })
  try {
    const response = await fetch('/api/python/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: input, user_id: 'demo', conv_id: conv.value || undefined }) })
    if (!response.ok) throw new Error(`请求失败：${response.status}`)
    const result = await response.json(); conv.value = result.conv_id
    rows.value.push({ role: '客服', content: result.response })
  } catch (e) { error.value = e.message } finally { busy.value = false }
}
</script>
<template>
  <main><h1>FinClario 客服工作台</h1><p>基础对话界面</p>
    <article v-for="(row, index) in rows" :key="index"><b>{{ row.role }}</b><p>{{ row.content }}</p></article>
    <form @submit.prevent="send"><input v-model="message" aria-label="问题" placeholder="请输入问题"><button :disabled="busy">{{ busy ? '处理中' : '发送' }}</button></form>
    <p role="alert">{{ error }}</p>
  </main>
</template>
