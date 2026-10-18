/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  // Neutral UI tokens. Legacy class names remain so data-heavy screens keep their behavior.
  theme: { extend: { colors: { paper:'#fafafa', ink:'#171717', forest:'#171717', moss:'#626262', line:'#e5e5e5', muted:'#6b6b6b', cream:'#f5f5f5', accent:'#555555' }, fontFamily: { display:['DM Sans Variable','sans-serif'], body:['DM Sans Variable','sans-serif'] }, boxShadow:{card:'0 1px 2px rgba(0,0,0,.04)'} } },
  plugins: []
};
