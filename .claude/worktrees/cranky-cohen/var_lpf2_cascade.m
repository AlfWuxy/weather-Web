function [y, state, a1, a2] = var_lpf2_cascade(x, fs, fc, state, method)
%VAR_LPF2_CASCADE Variable-coefficient 2nd-order LPF (cascade of two 1st-order LPFs)
%
%   [y, state, a1, a2] = var_lpf2_cascade(x, fs, fc, state, method)
%
% Inputs:
%   x      : Input signal vector
%   fs     : Sampling frequency (Hz), scalar > 0
%   fc     : Cutoff frequency definition (Hz)
%            - scalar: same cutoff for all samples and both stages
%            - Nx1   : time-varying cutoff, shared by both stages
%            - Nx2   : time-varying cutoff for each stage [fc1, fc2]
%   state  : (optional) struct with fields:
%            - y1: stage-1 previous output
%            - y2: stage-2 previous output
%            If omitted/empty, zero initial states are used.
%   method : (optional) 'exp' (default) or 'tustin'
%
% Outputs:
%   y      : Filtered output (same shape as x)
%   state  : Updated state (for block/stream processing)
%   a1,a2  : Per-sample recursive coefficients for stage-1 and stage-2
%
% Difference equations:
%   y1[n] = a1[n]*y1[n-1] + (1-a1[n])*x[n]
%   y2[n] = a2[n]*y2[n-1] + (1-a2[n])*y1[n]
%   y[n]  = y2[n]
%
% Notes:
%   - 'exp' mapping uses a = exp(-2*pi*fc/fs), robust for variable fc.
%   - fc is clamped to [0, 0.499*fs] for numerical safety.
%
% Example:
%   fs = 1000;
%   t = (0:fs-1)'/fs;
%   x = sin(2*pi*5*t) + 0.4*sin(2*pi*120*t);
%   fc = linspace(10, 40, numel(x))';
%   y = var_lpf2_cascade(x, fs, fc);
%

    narginchk(3, 5);

    if ~isvector(x)
        error('x must be a vector.');
    end
    if ~isscalar(fs) || ~isfinite(fs) || fs <= 0
        error('fs must be a finite positive scalar.');
    end

    if nargin < 4 || isempty(state)
        state = struct('y1', 0, 'y2', 0);
    else
        if ~isstruct(state) || ~isfield(state, 'y1') || ~isfield(state, 'y2')
            error('state must be a struct with fields y1 and y2.');
        end
        if ~isscalar(state.y1) || ~isscalar(state.y2)
            error('state.y1 and state.y2 must be scalars.');
        end
    end

    if nargin < 5 || isempty(method)
        method = 'exp';
    end

    x_is_row = isrow(x);
    x_col = x(:);
    N = numel(x_col);

    [fc1, fc2] = parse_fc(fc, N);

    fc_max = 0.499 * fs;
    fc1 = min(max(fc1, 0), fc_max);
    fc2 = min(max(fc2, 0), fc_max);

    switch lower(method)
        case {'exp', 'matchedz', 'matched'}
            a1 = exp(-2*pi*fc1/fs);
            a2 = exp(-2*pi*fc2/fs);
        case {'tustin', 'bilinear'}
            k1 = tan(pi*fc1/fs);
            k2 = tan(pi*fc2/fs);
            a1 = (1 - k1) ./ (1 + k1);
            a2 = (1 - k2) ./ (1 + k2);
        otherwise
            error("Unknown method '%s'. Use 'exp' or 'tustin'.", method);
    end

    b1 = 1 - a1;
    b2 = 1 - a2;

    y1 = state.y1;
    y2 = state.y2;
    y_col = zeros(N, 1, 'like', x_col);

    for n = 1:N
        y1 = a1(n) * y1 + b1(n) * x_col(n);
        y2 = a2(n) * y2 + b2(n) * y1;
        y_col(n) = y2;
    end

    state.y1 = y1;
    state.y2 = y2;

    if x_is_row
        y = y_col.';
        a1 = a1.';
        a2 = a2.';
    else
        y = y_col;
    end
end

function [fc1, fc2] = parse_fc(fc, N)
    if isscalar(fc)
        fc1 = repmat(fc, N, 1);
        fc2 = fc1;
        return;
    end

    if isvector(fc) && numel(fc) == N
        fc1 = fc(:);
        fc2 = fc1;
        return;
    end

    if ismatrix(fc) && size(fc, 1) == N && size(fc, 2) == 2
        fc1 = fc(:, 1);
        fc2 = fc(:, 2);
        return;
    end

    error('fc must be scalar, Nx1, or Nx2 (N = numel(x)).');
end
