import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthProvider'

function BusinessIcon({ kind }: { kind: 'products' | 'stock' | 'orders' | 'fx' | 'customers' }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    {kind === 'products'
      ? <><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z" /><path d="M3 7.5 12 12l9-4.5M12 12v9" /></>
      : kind === 'customers' ? <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></>
      : kind === 'stock'
        ? <><path d="M4 6h16v13H4zM4 10h16M8 3v3M16 3v3" /><path d="M9 14h6M9 17h4" /></>
        : kind === 'fx' ? <><circle cx="12" cy="12" r="9" /><path d="M7 12h10M12 7v10" /></>
        : <><path d="M5 3h14v18l-3-2-4 2-4-2-3 2z" /><path d="M8 8h8M8 12h8" /></>}
  </svg>
}

export function BusinessWorkspace() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const module = location.pathname.startsWith('/business/orders') ? 'orders'
    : location.pathname.startsWith('/business/customers') ? 'customers'
    : location.pathname.startsWith('/business/stock') ? 'stock'
      : location.pathname.startsWith('/business/fx') ? 'fx' : 'products'
  return <div className="business-workspace">
    <aside className="business-sidebar">
      <h2>Business</h2>
      <nav className="business-navigation" aria-label="Business navigation">
        <NavLink to="/business/products" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="products" />Products</NavLink>
        <NavLink to="/business/stock" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="stock" />Stock</NavLink>
        {auth.session?.capabilities.includes('order.read') && <NavLink to="/business/orders" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="orders" />Orders</NavLink>}
        {auth.session?.capabilities.includes('commerce.manage') && <NavLink to="/business/fx" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="fx" />FX</NavLink>}
        {module === 'customers' && <NavLink to={location.pathname} className="active" aria-current="page"><BusinessIcon kind="customers" />Customer</NavLink>}
      </nav>
    </aside>
    <div className="business-module-switcher">
      <label htmlFor="business-module">Business</label>
      <select id="business-module" value={module} onChange={(event) => navigate(`/business/${event.target.value}`)}>
        <option value="products">Products</option>
        <option value="stock">Stock</option>
        {auth.session?.capabilities.includes('order.read') && <option value="orders">Orders</option>}
        {auth.session?.capabilities.includes('commerce.manage') && <option value="fx">FX</option>}
        {module === 'customers' && <option value="customers" disabled>Customer profile</option>}
      </select>
    </div>
    <div className="business-content"><Outlet /></div>
  </div>
}
