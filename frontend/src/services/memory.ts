import request from './request';

export interface PreferenceValues {
  answer_style: 'concise' | 'normal' | 'detailed';
  knowledge_level: 'beginner' | 'intermediate' | 'expert';
  output_preferences: ('step_by_step' | 'table' | 'code_example' | 'principle_first')[];
}

export type PreferenceField = keyof PreferenceValues;
export interface Preferences {
  values: PreferenceValues;
  sources: Record<PreferenceField, 'default' | 'explicit'>;
  redis_fallback: boolean;
}

const path = '/memory/preferences';
const api = { baseURL: '/api' };

export const getPreferences = (): Promise<Preferences> => request.get(path, api);
export const setPreferences = (changes: Partial<PreferenceValues>): Promise<Preferences> =>
  request.put(path, changes, api);
export const clearPreferences = (field?: PreferenceField): Promise<Preferences> =>
  request.delete(field ? `${path}/${field}` : path, api);
