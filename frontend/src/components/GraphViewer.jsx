import { useEffect, useRef, useState } from 'react'
import cytoscape from 'cytoscape'
import { ZoomIn, ZoomOut, Maximize, ArrowUp, ArrowDown, ArrowLeft, ArrowRight, LocateFixed } from 'lucide-react'
import { graphElements, graphStyles } from '../utils/graph.js'
import { ErrorState } from './ErrorState.jsx'

export function GraphViewer({ graph, selectedId, onSelect }) {
  const container = useRef(null)
  const instance = useRef(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let cy
    let observer
    setError(false)
    try {
      cy = cytoscape({ container: container.current, elements: graphElements(graph), style: graphStyles,
        layout: { name: 'cose', animate: false, randomize: true, padding: 45, nodeDimensionsIncludeLabels: true, idealEdgeLength: 100, nodeRepulsion: 7000, numIter: 700 },
        minZoom: .1, maxZoom: 3, boxSelectionEnabled: false, autoungrabify: true })
      instance.current = cy
      cy.on('tap', 'node', (event) => onSelect(event.target.id()))
      cy.on('tap', (event) => { if (event.target === cy) onSelect(null) })
      observer = new ResizeObserver(() => { cy.resize(); cy.fit(undefined, 45) })
      observer.observe(container.current)
    } catch {
      setError(true) // Never expose library exceptions or graph payloads.
    }
    return () => { observer?.disconnect(); cy?.destroy(); instance.current = null }
  }, [graph, onSelect])
  useEffect(() => {
    const cy = instance.current
    if (!cy) return
    cy.nodes().unselect()
    if (selectedId) cy.getElementById(selectedId).select()
  }, [selectedId, graph])

  function zoom(factor) {
    const cy = instance.current
    if (cy) cy.zoom({ level: Math.max(cy.minZoom(), Math.min(cy.maxZoom(), cy.zoom() * factor)), renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
  }
  function pan(x, y) { instance.current?.panBy({ x, y }) }
  function fit() { instance.current?.fit(undefined, 45) }
  const controls = [
    ['Zoom in', ZoomIn, () => zoom(1.25)], ['Zoom out', ZoomOut, () => zoom(.8)],
    ['Fit network', Maximize, fit],
    ['Center selected node', LocateFixed, () => { if (selectedId) instance.current?.center(instance.current.getElementById(selectedId)) }],
    ['Pan left', ArrowLeft, () => pan(-50, 0)], ['Pan up', ArrowUp, () => pan(0, -50)],
    ['Pan down', ArrowDown, () => pan(0, 50)], ['Pan right', ArrowRight, () => pan(50, 0)],
  ]
  return <div className="graph-viewer">
    <div className="graph-controls" aria-label="Graph view controls">{controls.map(([name, Icon, action]) => <button key={name} type="button" className="icon-button" title={name} aria-label={name} onClick={action} disabled={error || (name === 'Center selected node' && !selectedId)}><Icon size={16} aria-hidden="true" /></button>)}</div>
    {error && <div className="resource-state"><ErrorState message="The graph could not be drawn. Try reloading it in a browser with canvas support." /></div>}
    <div ref={container} className="graph-canvas" role="img" aria-label="Read-only transaction relationship network. Use the node selector below for accessible node details." />
    <p className="graph-help">Scroll or pinch to zoom · Drag the background to pan · Click a node to inspect it</p>
  </div>
}
