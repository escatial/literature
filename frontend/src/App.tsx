import { HashRouter, Link, Route, Routes } from 'react-router-dom';
import { TopicInput } from './pages/TopicInput';
import { EnglishRetrieval } from './pages/EnglishRetrieval';
import { ChineseImport } from './pages/ChineseImport';
import { LiteraturePool } from './pages/LiteraturePool';
import { Writing } from './pages/Writing';

function App() {
  return (
    <HashRouter>
      <nav className="flex gap-4 p-4 bg-gray-100 border-b">
        <Link to="/">主题</Link>
        <Link to="/english">英文检索</Link>
        <Link to="/cn">中文导入</Link>
        <Link to="/pool">文献池</Link>
        <Link to="/writing">写作</Link>
      </nav>
      <main className="p-6 max-w-5xl mx-auto">
        <Routes>
          <Route path="/" element={<TopicInput />} />
          <Route path="/english" element={<EnglishRetrieval />} />
          <Route path="/cn" element={<ChineseImport />} />
          <Route path="/pool" element={<LiteraturePool />} />
          <Route path="/writing" element={<Writing />} />
        </Routes>
      </main>
    </HashRouter>
  );
}

export default App;
