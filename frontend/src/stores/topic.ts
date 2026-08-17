/** 全局主题状态(单一 topic,各页面共享)。*/
import { defineStore } from 'pinia';
import { readSharedTopic, writeSharedTopic } from './sharedTopic';

export const useTopicStore = defineStore('topic', {
  state: () => ({
    topic: readSharedTopic(),
  }),
  actions: {
    setTopic(t: string) {
      this.topic = t.trim();
      writeSharedTopic(this.topic);
    },
  },
});
