import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

function BusinessIcon({ kind }: { kind: 'products' | 'stock' }) {
  return <svg aria-hidden="true" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
    {kind === 'products'
      ? <><path d="M3 7.5 12 3l9 4.5v9L12 21l-9-4.5z" /><path d="M3 7.5 12 12l9-4.5M12 12v9" /></>
      : <><path d="M4 6h16v13H4zM4 10h16M8 3v3M16 3v3" /><path d="M9 14h6M9 17h4" /></>}
  </svg>
}

export function BusinessWorkspace() {
  const location = useLocation()
  const navigate = useNavigate()
  const module = location.pathname.startsWith('/business/stock') ? 'stock' : 'products'
  return <div className="business-workspace">
    <aside className="business-sidebar">
      <h2>Business</h2>
      <nav className="business-navigation" aria-label="Business navigation">
        <NavLink to="/business/products" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="products" />Products</NavLink>
        <NavLink to="/business/stock" className={({ isActive }) => isActive ? 'active' : undefined}><BusinessIcon kind="stock" />Stock</NavLink>
      </nav>
    </aside>
    <div className="business-module-switcher">
      <label htmlFor="business-module">Business</label>
      <select id="business-module" value={module} onChange={(event) => navigate(`/business/${event.target.value}`)}>
        <option value="products">Products</option>
        <option value="stock">Stock</option>
      </select>
    </div>
    <div className="business-content"><Outlet /></div>
  </div>
}
