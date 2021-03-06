import torch
from enum import Enum
# from block import block

import numpy as np
from scipy.linalg import lu_factor, lu_solve
from scipy.sparse import csc_matrix, eye, hstack, vstack, diags, block_diag, bmat
from scipy.sparse.linalg import splu, spsolve
from lcp_physics.lcp.util import get_sizes, bdiag


shown_btrifact_warning = False


def btrifact_hack(x):
    global shown_btrifact_warning
    try:
        return x.btrifact(pivot=not x.is_cuda)
    except TypeError:
        if not shown_btrifact_warning:
            print("""----------
lcp warning: Pivoting will always happen and will significantly
slow down your code. Please use the master branch of PyTorch
to get a version that disables pivoting on the GPU.
----------
""")
            shown_btrifact_warning = True
        return x.btrifact()


INACC_ERR = """
--------
lcp warning: Returning an inaccurate and potentially incorrect solutino.

Some residual is large.
Your problem may be infeasible or difficult.

You can try using the CVXPY solver to see if your problem is feasible
and you can use the verbose option to check the convergence status of
our solver while increasing the number of iterations.

Advanced users:
You can also try to enable iterative refinement in the solver:
https://github.com/locuslab/lcp/issues/6
--------
"""


class KKTSolvers(Enum):
    LU_FULL = 1
    SP_LU_FULL = 2
    LU_PARTIAL = 3
    IR_UNOPT = 4
    SP_IR_UNOPT = 5
    IR_INVERSE = 6
    SP_IR_INVERSE = 7


def forward(Q, p, G, h, A, b, F, Q_LU, S_LU, R,
            eps=1e-12, verbose=0, notImprovedLim=3,
            maxIter=20, solver=KKTSolvers.LU_PARTIAL):
    """
    Q_LU, S_LU, R = pre_factor_kkt(Q, G, A)
    """
    nineq, nz, neq, nBatch = get_sizes(G, A)

    # Find initial values
    if solver == KKTSolvers.LU_FULL:
        D = torch.eye(nineq).repeat(nBatch, 1, 1).type_as(Q)
        reg_eps = 1e-7
        Q_tilde = Q + reg_eps * torch.eye(nz).type_as(Q).repeat(nBatch, 1, 1)
        D_tilde = D + reg_eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)

        if neq > 0:
            A_ = torch.cat([torch.cat([G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2),
                            torch.cat([A, torch.zeros(nBatch, neq, nineq).type_as(Q_tilde)], 2)], 1)
        else:
            A_ = torch.cat([G, torch.eye(nineq).type_as(Q_tilde).unsqueeze(0)], 2)

        C_tilde = reg_eps * torch.eye(neq + nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)
        if F is not None:
            C_tilde[:, :nineq, :nineq] += F
        ns = [nineq, nz, neq, nBatch]
        x, s, z, y = factor_solve_kkt(
            Q_tilde, D_tilde, A_, C_tilde, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None, ns)
    elif solver == KKTSolvers.SP_LU_FULL:
        # TODO Have it work for batches
        D = eye(nineq, format='csc')
        reg_eps = 1e-7
        Q_tilde = csc_matrix(Q.squeeze(0).numpy()) + reg_eps * eye(nz, format='csc')
        D_tilde = D * (1 + reg_eps)

        if neq > 0:
            A_ = vstack([hstack([csc_matrix(G.squeeze(0).numpy()), eye(nineq, format='csc')], format='csc'),
                         hstack([csc_matrix(A.squeeze(0).numpy()), csc_matrix((neq, nineq))], format='csc')], format='csc')
        else:
            A_ = hstack([G, eye(nineq, format='csc')])

        # XXX
        C_tilde = reg_eps * np.eye(neq + nineq)
        if F is not None:
            C_tilde[:nineq, :nineq] += F.squeeze(0).numpy()
        C_tilde = csc_matrix(C_tilde)
        ns = [nineq, nz, neq, nBatch]
        x, s, z, y = sparse_factor_solve_kkt(
            Q_tilde, D_tilde, A_, C_tilde, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None, ns)
    elif solver == KKTSolvers.LU_PARTIAL:
        # XXX
        reg_eps = 1e-7
        d = torch.ones(nBatch, nineq).type_as(Q) # * (1 + reg_eps)
        factor_kkt(S_LU, R, d)
        x, s, z, y = solve_kkt(
            Q_LU, d, G, A, S_LU,
            p, torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if neq > 0 else None)
    elif solver == KKTSolvers.IR_UNOPT:
        D = torch.eye(nineq).repeat(nBatch, 1, 1).type_as(Q)
        x, s, z, y = solve_kkt_ir(
            Q, D, G, A, F, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None)
    elif solver == KKTSolvers.SP_IR_UNOPT:
        D = torch.eye(nineq).repeat(nBatch, 1, 1).type_as(Q)
        x, s, z, y = sparse_solve_kkt_ir(
            Q, D, G, A, F, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None)
    elif solver == KKTSolvers.IR_INVERSE:
        D = torch.eye(nineq).repeat(nBatch, 1, 1).type_as(Q)
        x, s, z, y = solve_kkt_ir_inverse(
            Q, D, G, A, F, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None)
    elif solver == KKTSolvers.SP_IR_INVERSE:
        reg_eps = 1e-7
        D = eye(nineq)
        D_tilde = D + reg_eps * eye(nineq)

        Q_tilde = csc_matrix(Q.squeeze(0).numpy()) + reg_eps * eye(nz)
        H_ = block_diag([Q_tilde, D_tilde], format='csc')
        if neq > 0:
            A_ = vstack([hstack([csc_matrix(G.squeeze(0).numpy()), eye(nineq, format='csc')], format='csc'),
                         hstack([csc_matrix(A.squeeze(0).numpy()), csc_matrix((neq, nineq))], format='csc')],
                        format='csc')
        else:
            A_ = hstack([G, eye(nineq, format='csc')])

        # TODO Test batche size > 1
        # XXX Shouldn't the sign below be positive? (Since its going to be subtracted later)
        C_tilde = -eps * eye(neq + nineq, format='csc')
        if F is not None:  # XXX inverted sign for F below
            C_tilde[:nineq, :nineq] -= F.squeeze(0).numpy()
        F_tilde = C_tilde[:nineq, :nineq]
        # C_tilde = csc_matrix(C_tilde.squeeze(0).numpy())

        x, s, z, y = sparse_solve_kkt_ir_inverse(H_, A_, C_tilde,
            Q_tilde, D_tilde, G, A, F_tilde, p,
            torch.zeros(nBatch, nineq).type_as(Q),
            -h, -b if b is not None else None)
    else:
        assert False

    M = torch.min(s, 1)[0].repeat(1, nineq)
    I = M <= 0
    s[I] -= M[I] - 1

    M = torch.min(z, 1)[0].repeat(1, nineq)
    I = M <= 0
    z[I] -= M[I] - 1

    best = {'resids': None, 'x': None, 'z': None, 's': None, 'y': None}
    nNotImproved = 0

    for i in range(maxIter):
        # affine scaling direction
        rx = (torch.bmm(y.unsqueeze(1), A).squeeze(1) if neq > 0 else 0.) + \
            torch.bmm(z.unsqueeze(1), G).squeeze(1) + \
            torch.bmm(x.unsqueeze(1), Q.transpose(1, 2)).squeeze(1) + \
            p
        rs = z
        rz = torch.bmm(x.unsqueeze(1), G.transpose(1, 2)).squeeze(1) + s - h
        if F is not None:  # XXX (Inverted sign for F below)
            rz -= torch.bmm(z.unsqueeze(1), F.transpose(1, 2)).squeeze(1)
        ry = torch.bmm(x.unsqueeze(1), A.transpose(
            1, 2)).squeeze(1) - b if neq > 0 else 0.0
        mu = torch.abs((s * z).sum(1).squeeze() / nineq)
        z_resid = torch.norm(rz, 2, 1).squeeze()
        y_resid = torch.norm(ry, 2, 1).squeeze() if neq > 0 else 0
        pri_resid = y_resid + z_resid
        dual_resid = torch.norm(rx, 2, 1).squeeze()
        resids = pri_resid + dual_resid + nineq * mu

        d = z / s
        if solver == KKTSolvers.LU_PARTIAL:
            try:
                factor_kkt(S_LU, R, d)
            except:
                return best['x'], best['y'], best['z'], best['s']

        if verbose == 1:
            print('iter: {}, pri_resid: {:.5e}, dual_resid: {:.5e}, mu: {:.5e}'.format(
                i, pri_resid.mean(), dual_resid.mean(), mu.mean()))
        if best['resids'] is None:
            best['resids'] = resids
            best['x'] = x.clone()
            best['z'] = z.clone()
            best['s'] = s.clone()
            best['y'] = y.clone() if y is not None else None
            nNotImproved = 0
        else:
            I = resids < best['resids']
            if I.sum() > 0:
                nNotImproved = 0
            else:
                nNotImproved += 1
            I_nz = I.repeat(nz, 1).t()
            I_nineq = I.repeat(nineq, 1).t()
            best['resids'][I] = resids[I]
            best['x'][I_nz] = x[I_nz]
            best['z'][I_nineq] = z[I_nineq]
            best['s'][I_nineq] = s[I_nineq]
            if neq > 0:
                I_neq = I.repeat(neq, 1).t()
                best['y'][I_neq] = y[I_neq]
        if nNotImproved == notImprovedLim or best['resids'].max() < eps or mu.min() > 1e100:
            if best['resids'].max() > 1. and verbose >= 0:
                print(INACC_ERR)
                print(best['resids'].max())
            return best['x'], best['y'], best['z'], best['s']

        if solver == KKTSolvers.LU_FULL:
            D = bdiag(d)
            D_tilde = D + reg_eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)
            dx_aff, ds_aff, dz_aff, dy_aff = factor_solve_kkt(
                Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns)
        elif solver == KKTSolvers.SP_LU_FULL:
            D = diags(d.squeeze(0).numpy())
            D_tilde = D + reg_eps * eye(nineq, format='csc')

            dx_aff, ds_aff, dz_aff, dy_aff = sparse_factor_solve_kkt(
                Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns)
        elif solver == KKTSolvers.LU_PARTIAL:
            dx_aff, ds_aff, dz_aff, dy_aff = solve_kkt(
                Q_LU, d, G, A, S_LU, rx, rs, rz, ry)
        elif solver == KKTSolvers.IR_UNOPT:
            D = bdiag(d)
            dx_aff, ds_aff, dz_aff, dy_aff = solve_kkt_ir(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.SP_IR_UNOPT:
            D = bdiag(d)
            dx_aff, ds_aff, dz_aff, dy_aff = sparse_solve_kkt_ir(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.IR_INVERSE:
            D = bdiag(d)
            dx_aff, ds_aff, dz_aff, dy_aff = solve_kkt_ir_inverse(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.SP_IR_INVERSE:
            D = diags(d.squeeze(0).numpy())
            D_tilde = D + reg_eps * eye(nineq)
            # H_ = block_diag([Q_tilde.squeeze(0).numpy(), D_tilde.squeeze(0).numpy()], format='csc')
            H_ = block_diag([Q_tilde, D_tilde], format='csc')
            dx_aff, ds_aff, dz_aff, dy_aff = sparse_solve_kkt_ir_inverse(H_, A_, C_tilde,
                Q_tilde, D_tilde, G, A, F_tilde, rx, rs, rz, ry)
        else:
            assert False

        # compute centering directions
        alpha = torch.min(torch.min(get_step(z, dz_aff),
                                    get_step(s, ds_aff)),
                          torch.ones(nBatch).type_as(Q))
        alpha_nineq = alpha.repeat(nineq, 1).t()
        t1 = s + alpha_nineq * ds_aff
        t2 = z + alpha_nineq * dz_aff
        t3 = torch.sum(t1 * t2, 1).squeeze()
        t4 = torch.sum(s * z, 1).squeeze()
        sig = (t3 / t4)**3

        rx = torch.zeros(nBatch, nz).type_as(Q)
        rs = ((-mu * sig).repeat(nineq, 1).t() + ds_aff * dz_aff) / s
        rz = torch.zeros(nBatch, nineq).type_as(Q)
        ry = torch.zeros(nBatch, neq).type_as(Q)

        if solver == KKTSolvers.LU_FULL:
            D = bdiag(d)
            D_tilde = D + reg_eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)
            dx_cor, ds_cor, dz_cor, dy_cor = factor_solve_kkt(
                Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns)
        elif solver == KKTSolvers.SP_LU_FULL:
            D = diags(d.squeeze(0).numpy())
            D_tilde = D + reg_eps * eye(nineq, format='csc')

            dx_cor, ds_cor, dz_cor, dy_cor = sparse_factor_solve_kkt(
                Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns)
        elif solver == KKTSolvers.LU_PARTIAL:
            dx_cor, ds_cor, dz_cor, dy_cor = solve_kkt(
                Q_LU, d, G, A, S_LU, rx, rs, rz, ry)
        elif solver == KKTSolvers.IR_UNOPT:
            D = bdiag(d)
            dx_cor, ds_cor, dz_cor, dy_cor = solve_kkt_ir(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.SP_IR_UNOPT:
            D = bdiag(d)
            dx_cor, ds_cor, dz_cor, dy_cor = sparse_solve_kkt_ir(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.IR_INVERSE:
            D = bdiag(d)
            dx_cor, ds_cor, dz_cor, dy_cor = solve_kkt_ir_inverse(
                Q, D, G, A, F, rx, rs, rz, ry)
        elif solver == KKTSolvers.SP_IR_INVERSE:
            D = diags(d.squeeze(0).numpy())
            D_tilde = D + reg_eps * eye(nineq)
            # H_ = block_diag([Q_tilde.squeeze(0).numpy(), D_tilde.squeeze(0).numpy()], format='csc')
            H_ = block_diag([Q_tilde, D_tilde], format='csc')
            dx_cor, ds_cor, dz_cor, dy_cor = sparse_solve_kkt_ir_inverse(H_, A_, C_tilde,
                Q_tilde, D_tilde, G, A, F_tilde, rx, rs, rz, ry)
        else:
            assert False

        dx = dx_aff + dx_cor
        ds = ds_aff + ds_cor
        dz = dz_aff + dz_cor
        dy = dy_aff + dy_cor if neq > 0 else None
        alpha = torch.min(0.999 * torch.min(get_step(z, dz),
                                            get_step(s, ds)),
                          torch.ones(nBatch).type_as(Q))
        alpha_nineq = alpha.repeat(nineq, 1).t()
        alpha_neq = alpha.repeat(neq, 1).t() if neq > 0 else None
        alpha_nz = alpha.repeat(nz, 1).t()

        x += alpha_nz * dx
        s += alpha_nineq * ds
        z += alpha_nineq * dz
        y = y + alpha_neq * dy if neq > 0 else None

    if best['resids'].max() > 1. and verbose >= 0:
        print(INACC_ERR)
        print(best['resids'].max())
    return best['x'], best['y'], best['z'], best['s']


def get_step(v, dv):
    a = -v / dv
    a[dv > 0] = max(1.0, a.max())
    step = a.min(1)[0].squeeze()
    return step


def unpack_kkt(v, nz, nineq, neq):
    i = 0
    x = v[:, i:i + nz]
    i += nz
    s = v[:, i:i + nineq]
    i += nineq
    z = v[:, i:i + nineq]
    i += nineq
    y = v[:, i:i + neq]
    return x, s, z, y


def kkt_resid_reg(Q_tilde, D_tilde, G, A, F_tilde, eps, dx, ds, dz, dy, rx, rs, rz, ry):
    dx, ds, dz, dy = [x.unsqueeze(2) if x is not None else None for x in [
        dx, ds, dz, dy]]
    resx = Q_tilde.bmm(dx) + G.transpose(1, 2).bmm(dz) + rx.unsqueeze(2)
    if dy is not None:
        resx += A.transpose(1, 2).bmm(dy)
    ress = D_tilde.bmm(ds) + dz + rs.unsqueeze(2)
    resz = G.bmm(dx) + ds + F_tilde.bmm(dz) + rz.unsqueeze(2)  # XXX
    resy = A.bmm(dx) - eps * dy + ry.unsqueeze(2) if dy is not None else None
    resx, ress, resz, resy = (
        v.squeeze(2) if v is not None else None for v in (resx, ress, resz, resy))

    return resx, ress, resz, resy


def solve_kkt_ir(Q, D, G, A, F, rx, rs, rz, ry, niter=1):
    """Inefficient iterative refinement."""
    nineq, nz, neq, nBatch = get_sizes(G, A)

    eps = 1e-7
    Q_tilde = Q + eps * torch.eye(nz).type_as(Q).repeat(nBatch, 1, 1)
    D_tilde = D + eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)

    # TODO Test batche size > 1
    # XXX Shouldn't the sign below be positive? (Since its going to be subtracted later)
    C_tilde = -eps * torch.eye(neq + nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)
    if F is not None:  # XXX inverted sign for F below
        C_tilde[:, :nineq, :nineq] -= F
    F_tilde = C_tilde[:, :nineq, :nineq]

    dx, ds, dz, dy = factor_solve_kkt_reg(
        Q_tilde, D_tilde, G, A, C_tilde, rx, rs, rz, ry, eps)
    resx, ress, resz, resy = kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                        dx, ds, dz, dy, rx, rs, rz, ry)
    for k in range(niter):
        ddx, dds, ddz, ddy = factor_solve_kkt_reg(Q_tilde, D_tilde, G, A, C_tilde,
                                                  -resx, -ress, -resz,
                                                  -resy if resy is not None else None,
                                                  eps)
        dx, ds, dz, dy = [v + dv if v is not None else None
                          for v, dv in zip((dx, ds, dz, dy), (ddx, dds, ddz, ddy))]
        resx, ress, resz, resy = kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                            dx, ds, dz, dy, rx, rs, rz, ry)

    return dx, ds, dz, dy


def factor_solve_kkt_reg(Q_tilde, D, G, A, C_tilde, rx, rs, rz, ry, eps):
    nineq, nz, neq, nBatch = get_sizes(G, A)

    H_ = torch.zeros(nBatch, nz + nineq, nz + nineq).type_as(Q_tilde)
    H_[:, :nz, :nz] = Q_tilde
    H_[:, -nineq:, -nineq:] = D
    if neq > 0:
        # H_ = torch.cat([torch.cat([Q, torch.zeros(nz,nineq).type_as(Q)], 1),
        # torch.cat([torch.zeros(nineq, nz).type_as(Q), D], 1)], 0)
        A_ = torch.cat([torch.cat([G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2),
                        torch.cat([A, torch.zeros(nBatch, neq, nineq).type_as(Q_tilde)], 2)], 1)
        g_ = torch.cat([rx, rs], 1)
        h_ = torch.cat([rz, ry], 1)
    else:
        A_ = torch.cat(
            [G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2)
        g_ = torch.cat([rx, rs], 1)
        h_ = rz

    H_LU = btrifact_hack(H_)

    invH_A_ = A_.transpose(1, 2).btrisolve(*H_LU)  # H-1 AT
    invH_g_ = g_.btrisolve(*H_LU)  # H-1 g

    S_ = torch.bmm(A_, invH_A_)  # A H-1 AT
    # A H-1 AT + C_tilde
    S_ -= C_tilde
    S_LU = btrifact_hack(S_)
    # [(H-1 g)T AT]T - h = A H-1 g - h
    t_ = torch.bmm(invH_g_.unsqueeze(1), A_.transpose(1, 2)).squeeze(1) - h_
    # w = (A H-1 AT + C_tilde)-1 (A H-1 g - h) <= Av - eps I w = h
    w_ = -t_.btrisolve(*S_LU)
    # XXX Shouldn't it be just g (no minus)?
    # (Doesn't seem to make a difference, though...)
    t_ = -g_ - w_.unsqueeze(1).bmm(A_).squeeze()  # -g - AT w
    v_ = t_.btrisolve(*H_LU)  # v = H-1 (-g - AT w)

    dx = v_[:, :nz]
    ds = v_[:, nz:]
    dz = w_[:, :nineq]
    dy = w_[:, nineq:] if neq > 0 else None

    return dx, ds, dz, dy


def sparse_kkt_resid_reg(Q_tilde, D_tilde, G, A, F_tilde, eps, dx, ds, dz, dy, rx, rs, rz, ry):
    dx, ds, dz, dy = [x.unsqueeze(2) if x is not None else None for x in [
        dx, ds, dz, dy]]
    resx = Q_tilde.bmm(dx) + G.transpose(1, 2).bmm(dz) + rx.unsqueeze(2)
    if dy is not None:
        resx += A.transpose(1, 2).bmm(dy)
    ress = D_tilde.bmm(ds) + dz + rs.unsqueeze(2)
    resz = G.bmm(dx) + ds + F_tilde.bmm(dz) + rz.unsqueeze(2)  # XXX
    resy = A.bmm(dx) - eps * dy + ry.unsqueeze(2) if dy is not None else None
    resx, ress, resz, resy = (
        v.squeeze(2) if v is not None else None for v in (resx, ress, resz, resy))

    return resx, ress, resz, resy


def sparse_solve_kkt_ir(Q, D, G, A, F, rx, rs, rz, ry, niter=1):
    """Inefficient iterative refinement."""
    nineq, nz, neq, nBatch = get_sizes(G, A)

    eps = 1e-7
    Q_tilde = Q + eps * torch.eye(nz).type_as(Q).repeat(nBatch, 1, 1)
    D_tilde = D + eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)

    # TODO Test batche size > 1
    # XXX Shouldn't the sign below be positive? (Since its going to be subtracted later)
    C_tilde = -eps * torch.eye(neq + nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)
    if F is not None:  # XXX inverted sign for F below
        C_tilde[:, :nineq, :nineq] -= F
    F_tilde = C_tilde[:, :nineq, :nineq]

    H_ = torch.zeros(nBatch, nz + nineq, nz + nineq).type_as(Q_tilde)
    H_[:, :nz, :nz] = Q_tilde
    H_[:, -nineq:, -nineq:] = D
    if neq > 0:
        # H_ = torch.cat([torch.cat([Q, torch.zeros(nz,nineq).type_as(Q)], 1),
        # torch.cat([torch.zeros(nineq, nz).type_as(Q), D], 1)], 0)
        A_ = torch.cat([torch.cat([G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2),
                        torch.cat([A, torch.zeros(nBatch, neq, nineq).type_as(Q_tilde)], 2)], 1)
    else:
        A_ = torch.cat(
            [G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2)
    spH_ = csc_matrix(H_.squeeze(0).numpy())
    A_ = A_.squeeze(0).numpy()
    spA_ = csc_matrix(A_)
    spC_tilde = csc_matrix(C_tilde.squeeze(0).numpy())

    dx, ds, dz, dy = sparse_factor_solve_kkt_reg(spH_, A_, spA_, spC_tilde,
                                                 rx, rs, rz, ry, neq, nineq, nz)
    resx, ress, resz, resy = sparse_kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                        dx, ds, dz, dy, rx, rs, rz, ry)
    for k in range(niter):
        ddx, dds, ddz, ddy = sparse_factor_solve_kkt_reg(spH_, A_, spA_, spC_tilde,
                                                  -resx, -ress, -resz,
                                                  -resy if resy is not None else None,
                                                  neq, nineq, nz)
        dx, ds, dz, dy = [v + dv if v is not None else None
                          for v, dv in zip((dx, ds, dz, dy), (ddx, dds, ddz, ddy))]
        resx, ress, resz, resy = sparse_kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                            dx, ds, dz, dy, rx, rs, rz, ry)

    return dx, ds, dz, dy


def sparse_factor_solve_kkt_reg(spH_, A_, spA_, spC_tilde, rx, rs, rz, ry, neq, nineq, nz):
    if neq > 0:
        g_ = torch.cat([rx, rs], 1)
        h_ = torch.cat([rz, ry], 1)
    else:
        g_ = torch.cat([rx, rs], 1)
        h_ = rz
    # XXX Not batched for now
    g_ = g_.squeeze(0).numpy()
    h_ = h_.squeeze(0).numpy()

    H_LU = splu(spH_)

    invH_A_ = H_LU.solve(A_.transpose())
    invH_g_ = H_LU.solve(g_)

    S_ = spA_.dot(invH_A_)  # A H-1 AT
    # A H-1 AT + C_tilde
    S_ -= spC_tilde
    # S_LU = btrifact_hack(S_)
    S_LU = lu_factor(S_)
    # [(H-1 g)T AT]T - h = A H-1 g - h
    t_ = spA_.dot(invH_g_) - h_
    # w = (A H-1 AT + C_tilde)-1 (A H-1 g - h) <= Av - eps I w = h
    # w_ = -t_.btrisolve(*S_LU)
    w_ = -lu_solve(S_LU, t_)
    # XXX Shouldn't it be just g (no minus)?
    # (Doesn't seem to make a difference, though...)
    t_ = -g_ - spA_.transpose().dot(w_)  # -g - AT w
    # v_ = t_.btrisolve(*H_LU)  # v = H-1 (-g - AT w)
    v_ = H_LU.solve(t_)

    dx = v_[:nz]
    ds = v_[nz:]
    dz = w_[:nineq]
    dy = w_[nineq:] if neq > 0 else None

    dx = torch.DoubleTensor(dx).unsqueeze(0)
    ds = torch.DoubleTensor(ds).unsqueeze(0)
    dz = torch.DoubleTensor(dz).unsqueeze(0)
    dy = torch.DoubleTensor(dy).unsqueeze(0) if neq > 0 else None

    return dx, ds, dz, dy


def solve_kkt_ir_inverse(Q, D, G, A, F, rx, rs, rz, ry, niter=1):
    """Inefficient iterative refinement."""
    nineq, nz, neq, nBatch = get_sizes(G, A)

    eps = 1e-7
    Q_tilde = Q + eps * torch.eye(nz).type_as(Q).repeat(nBatch, 1, 1)
    D_tilde = D + eps * torch.eye(nineq).type_as(Q).repeat(nBatch, 1, 1)

    # TODO Test batche size > 1
    # XXX Shouldn't the sign below be positive? (Since its going to be subtracted later)
    C_tilde = -eps * torch.eye(neq + nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)
    if F is not None:  # XXX inverted sign for F below
        C_tilde[:, :nineq, :nineq] -= F
    F_tilde = C_tilde[:, :nineq, :nineq]

    dx, ds, dz, dy = solve_kkt_inverse(
        Q_tilde, D_tilde, G, A, C_tilde, rx, rs, rz, ry, eps)
    resx, ress, resz, resy = kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                        dx, ds, dz, dy, rx, rs, rz, ry)
    for k in range(niter):
        ddx, dds, ddz, ddy = solve_kkt_inverse(Q_tilde, D_tilde, G, A, C_tilde,
                                               -resx, -ress, -resz,
                                               -resy if resy is not None else None,
                                               eps)
        dx, ds, dz, dy = [v + dv if v is not None else None
                          for v, dv in zip((dx, ds, dz, dy), (ddx, dds, ddz, ddy))]
        resx, ress, resz, resy = kkt_resid_reg(Q, D, G, A, F_tilde, eps,
                            dx, ds, dz, dy, rx, rs, rz, ry)

    return dx, ds, dz, dy


def solve_kkt_inverse(Q_tilde, D, G, A, C_tilde, rx, rs, rz, ry, eps):
    nineq, nz, neq, nBatch = get_sizes(G, A)

    H_ = torch.zeros(nBatch, nz + nineq, nz + nineq).type_as(Q_tilde)
    H_[:, :nz, :nz] = Q_tilde
    H_[:, -nineq:, -nineq:] = D
    if neq > 0:
        # H_ = torch.cat([torch.cat([Q, torch.zeros(nz,nineq).type_as(Q)], 1),
        # torch.cat([torch.zeros(nineq, nz).type_as(Q), D], 1)], 0)
        A_ = torch.cat([torch.cat([G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2),
                        torch.cat([A, torch.zeros(nBatch, neq, nineq).type_as(Q_tilde)], 2)], 1)
        g_ = torch.cat([rx, rs], 1)
        h_ = torch.cat([rz, ry], 1)
    else:
        A_ = torch.cat(
            [G, torch.eye(nineq).type_as(Q_tilde).repeat(nBatch, 1, 1)], 2)
        g_ = torch.cat([rx, rs], 1)
        h_ = rz

    full_mat = torch.cat([torch.cat([H_, A_.transpose(1,2)], 2),
                          torch.cat([A_, C_tilde], 2)], 1)
    full_res = torch.cat([g_, h_], 1)
    sol = torch.bmm(full_mat.squeeze(0).inverse().unsqueeze(0), full_res.unsqueeze(2)).squeeze(2)

    dx = sol[:, :nz]
    ds = sol[:, nz:nz+nineq]
    dz = sol[:, nz+nineq:nz+nineq+nineq]
    dy = sol[:, nz+nineq+nineq:] if neq > 0 else None

    return dx, ds, dz, dy


def sparse_solve_kkt_ir_inverse(H_, A_, C_tilde, Q_tilde, D_tilde, G, A, F_tilde,
                                rx, rs, rz, ry, niter=1):
    """Inefficient iterative refinement."""
    ns = nineq, nz, neq, nBatch = get_sizes(G, A)
    eps = 1e-7

    dx, ds, dz, dy = sparse_solve_kkt_inverse(
        H_, A_, C_tilde, rx, rs, rz, ry, ns)
    resx, ress, resz, resy = kkt_resid_reg(Q_tilde, D_tilde, G, A, F_tilde, eps,
                        dx, ds, dz, dy, rx, rs, rz, ry)
    for k in range(niter):
        ddx, dds, ddz, ddy = sparse_solve_kkt_inverse(H_, A_, C_tilde,
                                               -resx, -ress, -resz,
                                               -resy if resy is not None else None,
                                               ns)
        dx, ds, dz, dy = [v + dv if v is not None else None
                          for v, dv in zip((dx, ds, dz, dy), (ddx, dds, ddz, ddy))]
        resx, ress, resz, resy = kkt_resid_reg(Q_tilde, D_tilde, G, A, F_tilde, eps,
                            dx, ds, dz, dy, rx, rs, rz, ry)

    return dx, ds, dz, dy


def sparse_solve_kkt_inverse(H_, A_, C_tilde, rx, rs, rz, ry, ns):
    nineq, nz, neq, nBatch = ns

    if neq > 0:
        g_ = torch.cat([rx, rs], 1).squeeze(0).numpy()
        h_ = torch.cat([rz, ry], 1).squeeze(0).numpy()
    else:
        g_ = torch.cat([rx, rs], 1).squeeze(0).numpy()
        h_ = rz.squeeze(0).numpy()

    full_mat = bmat([[H_, A_.transpose()],
                     [A_, C_tilde]], format='csc')
    full_res = np.concatenate([g_, h_], 0)
    sol = splu(full_mat).solve(full_res)
    # sol = spsolve(full_mat, full_res)

    dx = sol[:nz]
    ds = sol[nz:nz+nineq]
    dz = sol[nz+nineq:nz+nineq+nineq]
    dy = sol[nz+nineq+nineq:] if neq > 0 else None

    dx = torch.DoubleTensor(dx).unsqueeze(0)
    ds = torch.DoubleTensor(ds).unsqueeze(0)
    dz = torch.DoubleTensor(dz).unsqueeze(0)
    dy = torch.DoubleTensor(dy).unsqueeze(0) if neq > 0 else None

    return dx, ds, dz, dy


def factor_solve_kkt(Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns):
    nineq, nz, neq, nBatch = ns

    H_ = torch.zeros(nBatch, nz + nineq, nz + nineq).type_as(Q_tilde)
    H_[:, :nz, :nz] = Q_tilde
    H_[:, -nineq:, -nineq:] = D_tilde
    if neq > 0:
        g_ = torch.cat([rx, rs], 1)
        h_ = torch.cat([rz, ry], 1)
    else:
        g_ = torch.cat([rx, rs], 1)
        h_ = rz

    H_LU = btrifact_hack(H_)

    invH_A_ = A_.transpose(1, 2).btrisolve(*H_LU)
    invH_g_ = g_.btrisolve(*H_LU)

    S_ = torch.bmm(A_, invH_A_) + C_tilde
    S_LU = btrifact_hack(S_)
    t_ = torch.bmm(invH_g_.unsqueeze(1), A_.transpose(1, 2)).squeeze(1) - h_
    w_ = -t_.btrisolve(*S_LU)
    t_ = -g_ - w_.unsqueeze(1).bmm(A_).squeeze()
    v_ = t_.btrisolve(*H_LU)

    dx = v_[:, :nz]
    ds = v_[:, nz:]
    dz = w_[:, :nineq]
    dy = w_[:, nineq:] if neq > 0 else None

    return dx, ds, dz, dy


def sparse_factor_solve_kkt(Q_tilde, D_tilde, A_, C_tilde, rx, rs, rz, ry, ns):
    nineq, nz, neq, nBatch = ns

    # H_ = csc_matrix((nz + nineq, nz + nineq))
    # H_[:nz, :nz] = Q_tilde
    # H_[-nineq:, -nineq:] = D_tilde
    H_ = block_diag([Q_tilde, D_tilde], format='csc')
    if neq > 0:
        g_ = torch.cat([rx, rs], 1).squeeze(0).numpy()
        h_ = torch.cat([rz, ry], 1).squeeze(0).numpy()
    else:
        g_ = torch.cat([rx, rs], 1).squeeze(0).numpy()
        h_ = rz.squeeze(0).numpy()

    H_LU = splu(H_)

    invH_A_ = csc_matrix(H_LU.solve(A_.todense().transpose()))
    invH_g_ = H_LU.solve(g_)

    S_ = A_.dot(invH_A_) + C_tilde
    S_LU = splu(S_)
    # t_ = invH_g_[np.newaxis].dot(A_.transpose()).squeeze(0) - h_
    t_ = A_.dot(invH_g_) - h_
    w_ = -S_LU.solve(t_)
    # t_ = -g_ - w_[np.newaxis].dot(A_).squeeze(0)
    t_ = -g_ - A_.transpose().dot(w_)
    v_ = H_LU.solve(t_)

    dx = v_[:nz]
    ds = v_[nz:]
    dz = w_[:nineq]
    dy = w_[nineq:] if neq > 0 else None

    dx = torch.DoubleTensor(dx).unsqueeze(0)
    ds = torch.DoubleTensor(ds).unsqueeze(0)
    dz = torch.DoubleTensor(dz).unsqueeze(0)
    dy = torch.DoubleTensor(dy).unsqueeze(0) if neq > 0 else None

    return dx, ds, dz, dy


def solve_kkt(Q_LU, d, G, A, S_LU, rx, rs, rz, ry):
    """ Solve KKT equations for the affine step"""

    # S = [ A Q^{-1} A^T        A Q^{-1} G^T          ]
    #     [ G Q^{-1} A^T        G Q^{-1} G^T + D^{-1} ]

    nineq, nz, neq, nBatch = get_sizes(G, A)

    invQ_rx = rx.btrisolve(*Q_LU)  # Q-1 rx
    if neq > 0:
        # A Q-1 rx - ry
        # G Q-1 rx + rs / d - rz
        h = torch.cat([invQ_rx.unsqueeze(1).bmm(A.transpose(1, 2)).squeeze(1) - ry,
                       invQ_rx.unsqueeze(1).bmm(G.transpose(1, 2)).squeeze(1) + rs / d - rz], 1)
    else:
        h = invQ_rx.unsqueeze(1).bmm(G.transpose(1, 2)).squeeze(1) + rs / d - rz

    w = -(h.btrisolve(*S_LU))  # S-1 h =

    g1 = -rx - w[:, neq:].unsqueeze(1).bmm(G).squeeze(1)  # -rx - GT w = -rx -GT S-1 h
    if neq > 0:
        g1 -= w[:, :neq].unsqueeze(1).bmm(A).squeeze(1)  # - AT w = -AT S-1 h
    g2 = -rs - w[:, neq:]

    dx = g1.btrisolve(*Q_LU)  # Q-1 g1 = - Q-1 AT S-1 h
    ds = g2 / d  # g2 / d = (-rs - w) / d
    dz = w[:, neq:]
    dy = w[:, :neq] if neq > 0 else None

    return dx, ds, dz, dy


def pre_factor_kkt(Q, G, F, A):
    """ Perform all one-time factorizations and cache relevant matrix products"""
    nineq, nz, neq, nBatch = get_sizes(G, A)

    try:
        Q_LU = btrifact_hack(Q)
    except:
        raise RuntimeError("""
lcp Error: Cannot perform LU factorization on Q.
Please make sure that your Q matrix is PSD and has
a non-zero diagonal.
""")

    # S = [ A Q^{-1} A^T        A Q^{-1} G^T          ]
    #     [ G Q^{-1} A^T        G Q^{-1} G^T + D^{-1} ]
    #
    # We compute a partial LU decomposition of the S matrix
    # that can be completed once D^{-1} is known.
    # See the 'Block LU factorization' part of our website
    # for more details.

    G_invQ_GT = torch.bmm(G, G.transpose(1, 2).btrisolve(*Q_LU)) + F
    R = G_invQ_GT.clone()
    S_LU_pivots = torch.IntTensor(range(1, 1 + neq + nineq)).unsqueeze(0) \
        .repeat(nBatch, 1).type_as(Q).int()
    if neq > 0:
        invQ_AT = A.transpose(1, 2).btrisolve(*Q_LU)
        A_invQ_AT = torch.bmm(A, invQ_AT)
        G_invQ_AT = torch.bmm(G, invQ_AT)

        LU_A_invQ_AT = btrifact_hack(A_invQ_AT)
        P_A_invQ_AT, L_A_invQ_AT, U_A_invQ_AT = torch.btriunpack(*LU_A_invQ_AT)
        P_A_invQ_AT = P_A_invQ_AT.type_as(A_invQ_AT)

        S_LU_11 = LU_A_invQ_AT[0]
        U_A_invQ_AT_inv = (P_A_invQ_AT.bmm(L_A_invQ_AT)
                           ).btrisolve(*LU_A_invQ_AT)
        S_LU_21 = G_invQ_AT.bmm(U_A_invQ_AT_inv)
        T = G_invQ_AT.transpose(1, 2).btrisolve(*LU_A_invQ_AT)
        S_LU_12 = U_A_invQ_AT.bmm(T)
        S_LU_22 = torch.zeros(nBatch, nineq, nineq).type_as(Q)
        S_LU_data = torch.cat((torch.cat((S_LU_11, S_LU_12), 2),
                               torch.cat((S_LU_21, S_LU_22), 2)),
                              1)
        S_LU_pivots[:, :neq] = LU_A_invQ_AT[1]

        R -= G_invQ_AT.bmm(T)
    else:
        S_LU_data = torch.zeros(nBatch, nineq, nineq).type_as(Q)

    S_LU = [S_LU_data, S_LU_pivots]
    return Q_LU, S_LU, R


factor_kkt_eye = None


def factor_kkt(S_LU, R, d):
    """ Factor the U22 block that we can only do after we know D. """
    nBatch, nineq = d.size()
    neq = S_LU[1].size(1) - nineq
    # TODO There's probably a better way to add a batched diagonal.
    global factor_kkt_eye
    if factor_kkt_eye is None or factor_kkt_eye.size() != d.size():
        # print('Updating batchedEye size.')
        factor_kkt_eye = torch.eye(nineq).repeat(
            nBatch, 1, 1).type_as(R).byte()
    T = R.clone()
    T[factor_kkt_eye] += (1. / d).squeeze()

    T_LU = btrifact_hack(T)

    global shown_btrifact_warning
    if shown_btrifact_warning or not T.is_cuda:
        # TODO Don't use pivoting in most cases because
        # torch.btriunpack is inefficient here:
        oldPivotsPacked = S_LU[1][:, -nineq:] - neq
        oldPivots, _, _ = torch.btriunpack(
            T_LU[0], oldPivotsPacked, unpack_data=False)
        newPivotsPacked = T_LU[1]
        newPivots, _, _ = torch.btriunpack(
            T_LU[0], newPivotsPacked, unpack_data=False)

        # Re-pivot the S_LU_21 block.
        if neq > 0:
            S_LU_21 = S_LU[0][:, -nineq:, :neq]
            S_LU[0][:, -nineq:,
                    :neq] = newPivots.transpose(1, 2).bmm(oldPivots.bmm(S_LU_21))

        # Add the new S_LU_22 block pivots.
        S_LU[1][:, -nineq:] = newPivotsPacked + neq

    # Add the new S_LU_22 block.
    S_LU[0][:, -nineq:, -nineq:] = T_LU[0]
