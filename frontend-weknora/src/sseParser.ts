// Keep partial lines (including a trailing CR) across transport chunks.
export function createSSEParser(emit: (type: string, data: string) => void) {
  let buffer = '';
  let type = '';
  let data: string[] = [];
  const dispatch = () => { if (data.length) emit(type, data.join('\n')); type = ''; data = []; };
  const line = (value: string) => {
    if (!value) { dispatch(); return; }
    if (value.startsWith(':')) return;
    const colon = value.indexOf(':');
    const field = colon < 0 ? value : value.slice(0, colon);
    let valueText = colon < 0 ? '' : value.slice(colon + 1);
    if (valueText.startsWith(' ')) valueText = valueText.slice(1);
    if (field === 'event') type = valueText;
    if (field === 'data') data.push(valueText);
  };
  return { push(chunk: string, eof = false) {
    buffer += chunk;
    while (buffer.length) {
      const end = buffer.search(/[\r\n]/);
      if (end < 0 || (!eof && end === buffer.length - 1 && buffer[end] === '\r')) break;
      const width = buffer[end] === '\r' && buffer[end + 1] === '\n' ? 2 : 1;
      line(buffer.slice(0, end)); buffer = buffer.slice(end + width);
    }
    if (eof) { if (buffer) line(buffer); buffer = ''; dispatch(); }
  } };
}
