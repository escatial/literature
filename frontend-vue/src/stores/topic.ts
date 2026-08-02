/** 全局主题状态(单一 topic,各页面共享)。*/
import { defineStore } from 'pinia';

export const useTopicStore = defineStore('topic', {
  state: () => ({
    topic: sessionStorage.getItem('lit_review_topic') ?? '',
  }),
  actions: {
    setTopic(t: string) {
      this.topic = t;
      sessionStorage.setItem('lit_review_topic', t);
    },
  },
});