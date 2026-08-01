/** 分类方式选择:国内外 / 主题。*/
import type { ClassifyMode } from '../hooks/useWriter';

interface Props {
  value: ClassifyMode;
  onChange: (v: ClassifyMode) => void;
  disabled?: boolean;
}

export function ClassifySelector({ value, onChange, disabled }: Props) {
  return (
    <div className="flex gap-4 items-center">
      <span className="text-sm font-medium">分类方式:</span>
      <label className="flex items-center gap-1 text-sm">
        <input
          type="radio"
          name="classify"
          value="locale"
          checked={value === 'locale'}
          onChange={() => onChange('locale')}
          disabled={disabled}
        />
        国内外分类
      </label>
      <label className="flex items-center gap-1 text-sm">
        <input
          type="radio"
          name="classify"
          value="theme"
          checked={value === 'theme'}
          onChange={() => onChange('theme')}
          disabled={disabled}
        />
        主题分类
      </label>
    </div>
  );
}
