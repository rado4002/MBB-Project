import { NavLink, Outlet } from 'react-router-dom'

export function BusinessWorkspace() {
  return <div className="business-workspace">
    <nav className="business-navigation" aria-label="Business navigation">
      <NavLink to="/business/products" className={({ isActive }) => isActive ? 'active' : undefined}>Products</NavLink>
      <NavLink to="/business/stock" className={({ isActive }) => isActive ? 'active' : undefined}>Stock</NavLink>
    </nav>
    <Outlet />
  </div>
}
