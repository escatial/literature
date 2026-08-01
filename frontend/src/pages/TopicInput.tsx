import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

export function TopicInput() {
  const nav = useNavigate();
  const [topic, setTopic] = useState('');

  useEffect(() => {
    const t = sessionStorage.getItem('lit_review_topic') || '';
    setTopic(t);
  }, []);

  function go() {
    if (!topic.trim()) return;
    sessionStorage.setItem('lit_review_topic', topic.trim());
    nav('/english');
  }

  return (
    <div className="max-w-xl">
      <h1 className="text-2xl font-bold mb-4">输入研究主题</h1>
      <p className="text-gray-600 mb-4">
        例:基于深度学习的医学影像诊断研究 / MBA 课程体系改革
      </p>
      <textarea
        className="w-full p-2 border rounded"
        rows={3}
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        placeholder="在这里输入你的研究主题..."
      />
      <button
        onClick={go}
        disabled={!topic.trim()}
        className="mt-3 px-4 py-2 bg-blue-600 text-white rounded disabled:opacity-50"
      >
        下一步:英文文献检索
      </button>
    </div>
  );
}
