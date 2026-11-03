import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Form, Radio, Space, Tag, Typography } from 'antd';
import { clearPreferences, getPreferences, setPreferences } from '@/services/memory';
import type { PreferenceField, Preferences, PreferenceValues } from '@/services/memory';

const styleOptions = [
  { value: 'concise', label: '简洁' }, { value: 'normal', label: '正常' }, { value: 'detailed', label: '详细' },
];
const levelOptions = [
  { value: 'beginner', label: '初学者' }, { value: 'intermediate', label: '中级' }, { value: 'expert', label: '专业' },
];
const outputOptions = [
  { value: 'step_by_step', label: '分步说明' }, { value: 'table', label: '表格' },
  { value: 'code_example', label: '代码示例' }, { value: 'principle_first', label: '先讲原理' },
];

export default function PreferenceCard() {
  const [snapshot, setSnapshot] = useState<Preferences>();
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const pending = useRef(false);

  async function sync(action: () => Promise<Preferences>, isWrite = false) {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setNotice('');
    try {
      setSnapshot(await action());
    } catch {
      setNotice(isWrite ? '未能确认偏好已保存，请重新读取。' : '无法读取偏好，请重试。');
    } finally {
      pending.current = false;
      setBusy(false);
    }
  }

  useEffect(() => { void sync(getPreferences); }, []);
  const controlsDisabled = busy || !snapshot || snapshot.redis_fallback || !!notice;
  const label = (field: PreferenceField, name: string) => (
    <Space>
      {name}
      <Tag color={snapshot?.sources[field] === 'explicit' ? 'blue' : undefined}>
        {snapshot?.sources[field] === 'explicit' ? '用户自定义' : '系统默认'}
      </Tag>
      {snapshot?.sources[field] === 'explicit' &&
        <Button type="link" size="small" aria-label={`恢复${name}默认值`}
          disabled={controlsDisabled} onClick={() => void sync(() => clearPreferences(field), true)}>恢复默认</Button>}
    </Space>
  );

  return (
    <Card title="长期回答偏好" style={{ marginBottom: 24 }}
      extra={<Button disabled={controlsDisabled || !Object.values(snapshot?.sources ?? {}).includes('explicit')}
        onClick={() => void sync(() => clearPreferences(), true)}>恢复全部默认值</Button>}>
      <Typography.Paragraph type="secondary">
        这里的选择会自动保存。你也可以在聊天中说“以后回答都简洁一点”。单次提问的要求优先。
      </Typography.Paragraph>
      {(notice || snapshot?.redis_fallback) &&
        <Alert type="warning" showIcon message={notice || '偏好存储暂时不可用，本次显示系统默认值。'}
          action={<Button size="small" onClick={() => void sync(getPreferences)} disabled={busy}>重新读取</Button>} />}
      {snapshot && <Form layout="vertical" component="div">
        <Form.Item label={label('answer_style', '回答风格')}>
          <Radio.Group aria-label="回答风格" disabled={controlsDisabled}
            value={snapshot.sources.answer_style === 'default' ? 'default' : snapshot.values.answer_style}
            options={[{ value: 'default', label: '默认（正常）' }, ...styleOptions]}
            onChange={({ target }) => void sync(() => target.value === 'default'
              ? clearPreferences('answer_style') : setPreferences({ answer_style: target.value }), true)} />
        </Form.Item>
        <Form.Item label={label('knowledge_level', '解释深度')}>
          <Radio.Group aria-label="解释深度" disabled={controlsDisabled}
            value={snapshot.sources.knowledge_level === 'default' ? 'default' : snapshot.values.knowledge_level}
            options={[{ value: 'default', label: '默认（中级）' }, ...levelOptions]}
            onChange={({ target }) => void sync(() => target.value === 'default'
              ? clearPreferences('knowledge_level') : setPreferences({ knowledge_level: target.value }), true)} />
        </Form.Item>
        <Form.Item label={label('output_preferences', '输出偏好')} style={{ marginBottom: 0 }}>
          <Checkbox.Group aria-label="输出偏好" disabled={controlsDisabled} options={outputOptions}
            value={snapshot.values.output_preferences}
            onChange={tags => void sync(() => setPreferences({
              output_preferences: tags as PreferenceValues['output_preferences'],
            }), true)} />
        </Form.Item>
      </Form>}
    </Card>
  );
}
