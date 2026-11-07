import { AppstoreOutlined, MessageOutlined, SettingOutlined, MenuOutlined, CloseOutlined, PlusOutlined, DeleteOutlined } from '@ant-design/icons';
import { useState } from 'react';
import { NavLink, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import KnowledgeBases from './pages/KnowledgeBases';
import Chat from './pages/Chat';
import Settings from './pages/Settings';
import { useChatHistory } from './chatHistory';

const navigation = [
  { to: '/knowledge-bases', label: '知识库', icon: <AppstoreOutlined /> },
  { to: '/settings', label: '模型与设置', icon: <SettingOutlined /> },
];

export default function App() {
  const [menuOpen, setMenuOpen] = useState(false);
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { sessions, remove, storageError } = useChatHistory();
  const current = pathname.startsWith('/chat') ? '智能问答' : navigation.find((item) => pathname.startsWith(item.to))?.label ?? '知识库';

  return (
    <div className="workspace">
      {menuOpen && <button className="sidebar-backdrop" aria-label="关闭导航" onClick={() => setMenuOpen(false)} />}
      <aside className={`workspace-sidebar ${menuOpen ? 'is-open' : ''}`}>
        <div className="workspace-brand">
          <div className="brand-mark" aria-hidden="true"><span /><span /><span /></div>
          <div><strong>知识工作台</strong><small>Knowledge Workspace</small></div>
          <button className="mobile-close" aria-label="关闭导航" onClick={() => setMenuOpen(false)}><CloseOutlined /></button>
        </div>

        <nav className="workspace-nav" aria-label="主导航">
          <button type="button" className={`nav-link new-chat ${pathname === '/chat' ? 'active' : ''}`} onClick={() => { navigate('/chat'); setMenuOpen(false); }}><span className="nav-icon"><PlusOutlined /></span><span>新对话</span></button>
          {navigation.map((item) => (
            <NavLink key={item.to} to={item.to} onClick={() => setMenuOpen(false)} className={({ isActive }) => `nav-link${isActive ? ' active' : ''}`}>
              <span className="nav-icon">{item.icon}</span><span>{item.label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="conversation-history" aria-label="历史会话">
          <div className="history-label">历史会话</div>
          {storageError && <p className="history-warning" role="alert">本地保存失败，当前会话可能无法保留</p>}
          {sessions.length === 0 && <p className="history-empty">开始对话后会显示在这里</p>}
          {sessions.slice().sort((a, b) => b.updatedAt - a.updatedAt).map(item => <div className={`history-item ${pathname === `/chat/${item.id}` ? 'active' : ''}`} key={item.id}>
            <button className="history-link" title={item.title} onClick={() => { navigate(`/chat/${item.id}`); setMenuOpen(false); }}><MessageOutlined /><span>{item.title}</span></button>
            <button className="history-delete" aria-label={`删除会话 ${item.title}`} onClick={() => { if (window.confirm(`删除会话“${item.title}”？`)) { remove(item.id); if (pathname === `/chat/${item.id}`) navigate('/chat'); } }}><DeleteOutlined /></button>
          </div>)}
        </div>
        <div className="sidebar-footer">
          <div className="footer-mark" aria-hidden="true">知</div>
          <div><strong>知识工作台</strong><small>让知识更容易被使用</small></div>
        </div>
      </aside>
      <div className="workspace-main">
        <header className="workspace-topbar">
          <button className="mobile-menu" aria-label="打开导航" onClick={() => setMenuOpen(true)}><MenuOutlined /></button>
          <span className="topbar-root">工作空间</span><span className="topbar-divider">/</span><strong>{current}</strong>
        </header>
        <main className="workspace-content">
          <Routes>
            <Route path="/" element={<Navigate to="/knowledge-bases" replace />} />
            <Route path="/knowledge-bases/*" element={<KnowledgeBases />} />
            <Route path="/chat/:id?" element={<Chat />} />
            <Route path="/settings/*" element={<Settings />} />
            <Route path="*" element={<Navigate to="/knowledge-bases" replace />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
