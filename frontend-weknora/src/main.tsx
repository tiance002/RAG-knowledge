import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App';
import { ChatHistoryProvider } from './chatHistory';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#079b57', borderRadius: 8, fontFamily: '"Microsoft YaHei", "Noto Sans SC", system-ui, sans-serif' } }}>
      <BrowserRouter><ChatHistoryProvider><App /></ChatHistoryProvider></BrowserRouter>
    </ConfigProvider>
  </React.StrictMode>,
);
