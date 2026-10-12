import { LayoutDashboard, ArrowLeftRight, Network, ShieldCheck } from 'lucide-react'

export const navigation = [
  { path: '/', title: 'Overview', icon: LayoutDashboard },
  { path: '/transactions', title: 'Transactions', icon: ArrowLeftRight },
  { path: '/graph-explorer', title: 'Graph Explorer', icon: Network },
  { path: '/investigations', title: 'Investigations', icon: ShieldCheck },
]
