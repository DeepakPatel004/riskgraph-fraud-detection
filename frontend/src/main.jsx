import { createRoot } from 'react-dom/client'
import { App } from './App.jsx'
import './styles.css'

// Keep the health effect single-run in development too; it cleans up on unmount.
createRoot(document.getElementById('root')).render(<App />)
