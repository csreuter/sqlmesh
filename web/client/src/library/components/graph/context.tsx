import { type Column } from '@api/client'
import { useStoreContext } from '@context/context'
import { type Lineage } from '@context/editor'
import { type ModelSQLMeshModel } from '@models/sqlmesh-model'
import {
  createContext,
  useState,
  useContext,
  useCallback,
  useMemo,
} from 'react'
import { hasActiveEdge, hasActiveEdgeConnector } from './help'
import { type ErrorIDE } from '~/library/pages/ide/context'
import { EnumSide } from '~/types/enum'
import { isFalse, toID } from '@utils/index'
import { type ConnectedNode } from '~/workers/lineage'

export interface Connections {
  left: string[]
  right: string[]
}
export type ActiveColumns = Map<string, { ins: string[]; outs: string[] }>
export type ActiveEdges = Map<string, Array<[string, string]>>
export type ActiveNodes = Set<string>
export type SelectedNodes = Set<string>
export type HighlightedNodes = Record<string, string[]>

interface LineageFlow {
  lineage?: Record<string, Lineage>
  mainNode?: string
  connectedNodes: Set<string>
  activeEdges: ActiveEdges
  activeNodes: ActiveNodes
  selectedNodes: SelectedNodes
  selectedEdges: ConnectedNode[]
  models: Map<string, ModelSQLMeshModel>
  connections: Map<string, Connections>
  withConnected: boolean
  withColumns: boolean
  hasBackground: boolean
  withImpacted: boolean
  withSecondary: boolean
  manuallySelectedColumn?: [ModelSQLMeshModel, Column]
  highlightedNodes: HighlightedNodes
  setHighlightedNodes: React.Dispatch<React.SetStateAction<HighlightedNodes>>
  setActiveNodes: React.Dispatch<React.SetStateAction<ActiveNodes>>
  setWithConnected: React.Dispatch<React.SetStateAction<boolean>>
  setMainNode: React.Dispatch<React.SetStateAction<string | undefined>>
  setSelectedNodes: React.Dispatch<React.SetStateAction<SelectedNodes>>
  setWithColumns: React.Dispatch<React.SetStateAction<boolean>>
  setHasBackground: React.Dispatch<React.SetStateAction<boolean>>
  setWithImpacted: React.Dispatch<React.SetStateAction<boolean>>
  setWithSecondary: React.Dispatch<React.SetStateAction<boolean>>
  setConnections: React.Dispatch<React.SetStateAction<Map<string, Connections>>>
  hasActiveEdge: (edge: [Maybe<string>, Maybe<string>]) => boolean
  addActiveEdges: (edges: Array<[string, string]>) => void
  removeActiveEdges: (edges: Array<[string, string]>) => void
  setActiveEdges: React.Dispatch<React.SetStateAction<ActiveEdges>>
  setLineage: React.Dispatch<React.SetStateAction<Record<string, Lineage>>>
  handleClickModel?: (modelName: string) => void
  handleError?: (error: ErrorIDE) => void
  setManuallySelectedColumn: React.Dispatch<
    React.SetStateAction<[ModelSQLMeshModel, Column] | undefined>
  >
  setNodeConnections: React.Dispatch<Record<string, ConnectedNode>>
  isActiveColumn: (modelName: string, columnName: string) => boolean
}

export const LineageFlowContext = createContext<LineageFlow>({
  selectedEdges: [],
  lineage: {},
  withColumns: false,
  withConnected: false,
  withImpacted: true,
  withSecondary: false,
  hasBackground: false,
  mainNode: undefined,
  activeEdges: new Map(),
  activeNodes: new Set(),
  models: new Map(),
  manuallySelectedColumn: undefined,
  connections: new Map(),
  selectedNodes: new Set(),
  connectedNodes: new Set(),
  highlightedNodes: {},
  setHighlightedNodes: () => {},
  setWithColumns: () => false,
  setHasBackground: () => false,
  setWithImpacted: () => false,
  setWithSecondary: () => false,
  setWithConnected: () => false,
  hasActiveEdge: () => false,
  addActiveEdges: () => {},
  removeActiveEdges: () => {},
  setActiveEdges: () => {},
  handleClickModel: () => {},
  setManuallySelectedColumn: () => {},
  handleError: () => {},
  setLineage: () => {},
  isActiveColumn: () => false,
  setConnections: () => {},
  setSelectedNodes: () => {},
  setMainNode: () => {},
  setActiveNodes: () => {},
  setNodeConnections: () => {},
})

export default function LineageFlowProvider({
  handleError,
  handleClickModel,
  children,
  withColumns = false,
}: {
  children: React.ReactNode
  handleClickModel?: (modelName: string) => void
  handleError?: (error: ErrorIDE) => void
  withColumns?: boolean
}): JSX.Element {
  const models = useStoreContext(s => s.models)

  const [lineage, setLineage] = useState<Record<string, Lineage>>({})
  const [nodesConnections, setNodeConnections] = useState<
    Record<string, ConnectedNode>
  >({})
  const [hasColumns, setWithColumns] = useState(withColumns)
  const [mainNode, setMainNode] = useState<string>()
  const [manuallySelectedColumn, setManuallySelectedColumn] =
    useState<[ModelSQLMeshModel, Column]>()
  const [activeEdges, setActiveEdges] = useState<ActiveEdges>(new Map())
  const [connections, setConnections] = useState<Map<string, Connections>>(
    new Map(),
  )
  const [withConnected, setWithConnected] = useState(false)
  const [selectedNodes, setSelectedNodes] = useState<SelectedNodes>(new Set())
  const [activeNodes, setActiveNodes] = useState<ActiveNodes>(new Set())
  const [highlightedNodes, setHighlightedNodes] = useState<HighlightedNodes>({})
  const [hasBackground, setHasBackground] = useState(false)
  const [withImpacted, setWithImpacted] = useState(true)
  const [withSecondary, setWithSecondary] = useState(false)

  const checkActiveEdge = useCallback(
    function checkActiveEdge(edge: [Maybe<string>, Maybe<string>]): boolean {
      return hasActiveEdge(activeEdges, edge)
    },
    [activeEdges],
  )

  const addActiveEdges = useCallback(
    function addActiveEdges(edges: Array<[string, string]>): void {
      setActiveEdges(activeEdges => {
        edges.forEach(([leftConnect, rightConnect]) => {
          const left = activeEdges.get(leftConnect) ?? []
          const right = activeEdges.get(rightConnect) ?? []
          const hasDuplicateLeft = left.some(
            ([left, right]) => left === leftConnect && right === rightConnect,
          )
          const hasDuplicateRight = right.some(
            ([left, right]) => left === leftConnect && right === rightConnect,
          )

          if (isFalse(hasDuplicateLeft)) {
            left.push([leftConnect, rightConnect])
          }

          if (isFalse(hasDuplicateRight)) {
            right.push([leftConnect, rightConnect])
          }

          activeEdges.set(leftConnect, left)
          activeEdges.set(rightConnect, right)
        })

        return new Map(activeEdges)
      })
    },
    [setActiveEdges],
  )

  const removeActiveEdges = useCallback(
    function removeActiveEdges(edges: Array<[string, string]>): void {
      setActiveEdges(activeEdges => {
        edges.forEach(([left, right]) => {
          const edgesLeft = (activeEdges.get(left) ?? []).filter(
            e => e[0] !== left && e[1] !== right,
          )
          const edgesRight = (activeEdges.get(right) ?? []).filter(
            e => e[0] !== left && e[1] !== right,
          )

          activeEdges.set(left, edgesLeft)
          activeEdges.set(right, edgesRight)
        })

        return new Map(activeEdges)
      })

      setConnections(connections => {
        edges.forEach(([left, right]) => {
          connections.delete(left)
          connections.delete(right)
        })

        return new Map(connections)
      })
    },
    [setActiveEdges, setConnections],
  )

  const isActiveColumn = useCallback(
    function isActive(modelName: string, columnName: string): boolean {
      const leftConnector = toID(EnumSide.Left, modelName, columnName)
      const rightConnector = toID(EnumSide.Right, modelName, columnName)

      return (
        hasActiveEdgeConnector(activeEdges, leftConnector) ||
        hasActiveEdgeConnector(activeEdges, rightConnector)
      )
    },
    [checkActiveEdge, activeEdges],
  )

  const connectedNodes: Set<string> = useMemo(
    () => new Set(Object.keys(nodesConnections)),
    [nodesConnections],
  )

  const selectedEdges = useMemo(
    () =>
      Array.from(selectedNodes)
        .map(id => nodesConnections[id]!)
        .flat(),
    [nodesConnections, selectedNodes],
  )

  return (
    <LineageFlowContext.Provider
      value={{
        highlightedNodes,
        setHighlightedNodes,
        connectedNodes,
        activeEdges,
        selectedEdges,
        activeNodes,
        setActiveNodes,
        setNodeConnections,
        selectedNodes,
        mainNode,
        connections,
        lineage,
        models,
        manuallySelectedColumn,
        withColumns: hasColumns,
        withConnected,
        withImpacted,
        withSecondary,
        hasBackground,
        setWithConnected,
        setWithImpacted,
        setWithSecondary,
        setHasBackground,
        setSelectedNodes,
        setMainNode,
        setConnections,
        setLineage,
        setWithColumns,
        setActiveEdges,
        setManuallySelectedColumn,
        addActiveEdges,
        removeActiveEdges,
        hasActiveEdge: checkActiveEdge,
        handleClickModel,
        handleError,
        isActiveColumn,
      }}
    >
      {children}
    </LineageFlowContext.Provider>
  )
}

export function useLineageFlow(): LineageFlow {
  return useContext(LineageFlowContext)
}
